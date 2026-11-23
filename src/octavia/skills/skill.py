"""A skill: named steps of a task's tools, with inputs filled in each time it's used, and where skills are kept.

A skill is kept in her long-term memory beside the playbooks (``memory.base.APART``): its category is
"playbook:skill:<name>" and its text is the skill as JSON. Every change to it (the user's yes to a proposal, turning it
off, an older version brought back) is a correction, so the version before stays as history (``KEEP_VERSIONS``) and
``forget`` removes it with all of them. Kept apart from the facts, it never shows up in her prompt's pick, recall or
the list of what she knows about the user.

How it's been doing (runs, clean runs, failures in a row, the last error) changes with every use, so it's kept apart
from the versions, in a small file (``Stats``), counted afresh for each version. So is what she was told no to
(``declined``), so a repeat the user turned down isn't offered again soon.

What it's called, from what's stored and what's counted (``Skill.status``):

* "proposed": waiting for the user's yes; it never runs.
* "trial": the user said yes and its checks passed, but it hasn't had a clean real run yet.
* "ready": it has had at least one.
* "off": turned off (it failed too often in a row, or the user's ``save_skill`` hasn't turned it back on); it never
  runs.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from octavia.core.jsonfile import read_json, write_json
from octavia.memory.base import APART, LongTermMemory, MemoryItem
from octavia.skills.rules import fill, inputs_used  # noqa: F401  (part of what a skill is, kept with its rules)

log = logging.getLogger(__name__)

PREFIX = f"{APART}skill:"
KEEP_VERSIONS = 10  # older versions kept as history
DECLINED_DAYS = 30  # a repeat turned down isn't offered again for this long


@dataclass(frozen=True)
class Input:
    name: str
    about: str = ""


@dataclass(frozen=True)
class Step:
    """One step: a call of a task tool (``tool``, ``args``, whose text may hold ``{input}``), or another skill
    (``skill``, with ``inputs`` for it). ``expect``: words the step's result must contain for it to count."""

    tool: str = ""
    args: Mapping[str, Any] = field(default_factory=dict)
    expect: str = ""
    skill: str = ""
    inputs: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        if self.skill:
            return {"skill": self.skill, "inputs": dict(self.inputs)}
        out: dict[str, Any] = {"tool": self.tool, "args": dict(self.args)}
        if self.expect:
            out["expect"] = self.expect
        return out

    def words(self) -> str:
        """The step in a few words: "click label=Design", "the skill open_course"."""
        if self.skill:
            return f"the skill {self.skill}"
        shown = " ".join(f"{k}={v}" for k, v in self.args.items() if v not in (None, "", False))
        return f"{self.tool} {shown}".strip()


@dataclass(frozen=True)
class Skill:
    name: str
    title: str
    kind: str  # "screen task" | "web task"
    steps: tuple[Step, ...]
    purpose: str = ""
    place: str = ""  # the app or site it works in (a playbook's key), "" for anywhere
    inputs: tuple[Input, ...] = ()
    state: str = "proposed"  # "proposed" | "approved" | "off"
    origin: str = "task"  # "task" (a task worked it out) | "repeats" (done by hand again and again) | "user"
    version: int = 1
    signature: str = ""  # of the repeat it came from, so the same one isn't offered twice
    off_reason: str = ""
    proposed_at: float = 0.0
    # Stored elsewhere: the memory note's id and time, and how it's been doing (``Stats``).
    id: int = 0
    updated_at: float = 0.0
    runs: int = 0
    clean_runs: int = 0
    fails_in_row: int = 0
    last_error: str = ""

    @property
    def status(self) -> str:
        if self.state != "approved":
            return self.state
        return "ready" if self.clean_runs else "trial"

    @property
    def usable(self) -> bool:
        return self.state == "approved"

    @property
    def category(self) -> str:
        return PREFIX + self.name

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "title": self.title,
            "kind": self.kind,
            "purpose": self.purpose,
            "place": self.place,
            "inputs": [{"name": i.name, "about": i.about} for i in self.inputs],
            "steps": [s.to_dict() for s in self.steps],
            "state": self.state,
            "origin": self.origin,
            "version": self.version,
        }
        for key in ("signature", "off_reason", "proposed_at"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        return out

    def text(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    def uses(self) -> str:
        """How it's called: "open_course(course)"."""
        return f"{self.name}({', '.join(i.name for i in self.inputs)})"


def _str(value: Any, limit: int) -> str:
    return " ".join(str(value).split())[:limit] if isinstance(value, (str, int, float)) else ""


def step_from(data: Any) -> Step:
    """A step from its JSON (what a task proposed, or what's stored). Raises ValueError when it isn't one."""
    if not isinstance(data, Mapping):
        raise ValueError("a step must be an object")
    if data.get("skill"):
        inputs = data.get("inputs") or {}
        if not isinstance(inputs, Mapping):
            raise ValueError("a skill step's inputs must be an object")
        return Step(skill=_str(data["skill"], 60), inputs={str(k): str(v) for k, v in inputs.items()})
    args = data.get("args") or {}
    if not isinstance(args, Mapping):
        raise ValueError("a step's args must be an object")
    return Step(tool=_str(data.get("tool", ""), 60), args=dict(args), expect=_str(data.get("expect", ""), 200))


def skill_from(data: Any) -> Skill:
    """A skill from its JSON. Raises ValueError when it isn't one (``rules.check`` judges what's in it)."""
    if not isinstance(data, Mapping):
        raise ValueError("a skill must be an object")
    raw_inputs = data.get("inputs") or []
    raw_steps = data.get("steps") or []
    if not isinstance(raw_inputs, list) or not isinstance(raw_steps, list):
        raise ValueError("a skill's inputs and steps must be lists")
    inputs = []
    for item in raw_inputs:
        if isinstance(item, str):
            inputs.append(Input(item.strip()))
        elif isinstance(item, Mapping):
            inputs.append(Input(_str(item.get("name", ""), 40), _str(item.get("about", ""), 200)))
        else:
            raise ValueError("an input must be a name or an object")
    version = data.get("version", 1)
    proposed_at = data.get("proposed_at", 0.0)
    return Skill(
        name=_str(data.get("name", ""), 60),
        title=_str(data.get("title", ""), 200),
        kind=_str(data.get("kind", ""), 20),
        steps=tuple(step_from(s) for s in raw_steps),
        purpose=_str(data.get("purpose", ""), 400),
        place=_str(data.get("place", ""), 80).casefold(),
        inputs=tuple(inputs),
        state=_str(data.get("state", "proposed"), 20),
        origin=_str(data.get("origin", "task"), 20),
        version=version if isinstance(version, int) and not isinstance(version, bool) and version > 0 else 1,
        signature=_str(data.get("signature", ""), 80),
        off_reason=_str(data.get("off_reason", ""), 300),
        proposed_at=float(proposed_at) if isinstance(proposed_at, (int, float)) else 0.0,
    )


def _from_item(item: MemoryItem) -> Skill | None:
    if not item.category.startswith(PREFIX):
        return None
    try:
        skill = skill_from(json.loads(item.text))
    except ValueError:
        log.warning("Skill note %s isn't a skill any more; left out", item.id)
        return None
    if skill.name != item.category[len(PREFIX) :]:
        return None
    return replace(skill, id=item.id, updated_at=item.updated_at)


class Stats:
    """How each skill's current version has been doing, and the repeats the user turned down (a JSON file)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._data: dict[str, Any] | None = None if path is not None else {}

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            found = read_json(self.path, "the skills' record") if self.path is not None else None
            self._data = found if isinstance(found, dict) else {}
        return self._data

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            write_json(self.path, self._load(), indent=1)
        except OSError:
            log.warning("Couldn't write the skills' record to %s", self.path, exc_info=True)

    def of(self, name: str, version: int) -> dict[str, Any]:
        entry = self._load().get("skills", {}).get(name)
        return entry if isinstance(entry, dict) and entry.get("version") == version else {}

    def record(self, name: str, version: int, ok: bool, error: str = "") -> dict[str, Any]:
        """Count one run of ``name`` at ``version``; returns its counts so far."""
        skills = self._load().setdefault("skills", {})
        entry = self.of(name, version) or {"version": version, "runs": 0, "clean": 0, "fails_in_row": 0}
        entry["runs"] = entry.get("runs", 0) + 1
        entry["last_used"] = time.time()
        if ok:
            entry["clean"] = entry.get("clean", 0) + 1
            entry["fails_in_row"] = 0
        else:
            entry["fails_in_row"] = entry.get("fails_in_row", 0) + 1
            entry["last_error"] = error[:300]
        skills[name] = entry
        self._save()
        return entry

    def drop(self, name: str) -> None:
        if self._load().get("skills", {}).pop(name, None) is not None:
            self._save()

    def decline(self, signature: str, now: float | None = None) -> None:
        if not signature:
            return
        self._load().setdefault("declined", {})[signature] = now if now is not None else time.time()
        self._save()

    def declined(self, now: float | None = None) -> set[str]:
        now = now if now is not None else time.time()
        found = self._load().get("declined", {})
        return {sig for sig, at in found.items() if isinstance(at, (int, float)) and now - at < DECLINED_DAYS * 86400}


class Skills:
    """The skills in her long-term memory, with how each has been doing."""

    def __init__(self, memory: LongTermMemory, stats: Stats | None = None) -> None:
        self.memory = memory
        self.stats = stats or Stats(None)

    def _with_stats(self, skill: Skill) -> Skill:
        entry = self.stats.of(skill.name, skill.version)
        return replace(
            skill,
            runs=int(entry.get("runs", 0)),
            clean_runs=int(entry.get("clean", 0)),
            fails_in_row=int(entry.get("fails_in_row", 0)),
            last_error=str(entry.get("last_error", "")),
        )

    async def all(self) -> list[Skill]:
        """Newest first."""
        found = [_from_item(item) for item in await self.memory.list_apart(PREFIX)]
        return [self._with_stats(skill) for skill in found if skill is not None]

    async def get(self, name: str) -> Skill | None:
        name = name.strip().casefold()
        return next((skill for skill in await self.all() if skill.name == name), None)

    async def save(self, skill: Skill, session_id: str | None = None) -> Skill:
        """Write ``skill`` as its newest version (the one before kept as history). Its version number goes up by one
        from the one stored, whatever it says."""
        old = await self.get(skill.name)
        if old is None:
            new = replace(skill, version=1)
            item = await self.memory.add(new.text(), new.category, source="observed", session_id=session_id)
        else:
            new = replace(skill, version=old.version + 1)
            item = await self.memory.correct(old.id, new.text(), session_id=session_id, source="observed")
            if item is None:  # forgotten meanwhile
                item = await self.memory.add(new.text(), new.category, source="observed", session_id=session_id)
            await self.memory.trim_history(item.id, KEEP_VERSIONS)
        saved = _from_item(item)
        if saved is None:  # never: it was just written from a skill
            raise ValueError(f"the skill {skill.name} couldn't be read back")
        return self._with_stats(saved)

    async def versions(self, name: str) -> list[Skill]:
        """The skill's versions, newest first (the current one first)."""
        current = await self.get(name)
        if current is None:
            return []
        found = [_from_item(item) for item in await self.memory.history(current.id)]
        return [skill for skill in found if skill is not None]

    async def forget(self, name: str) -> Skill | None:
        """Delete the skill with its history; returns what was deleted. One that came from a repeat isn't offered
        again soon (a proposal deleted is a "no" to it; a saved one deleted, a "no" to having it)."""
        skill = await self.get(name)
        if skill is None or not await self.memory.delete(skill.id):
            return None
        self.stats.drop(skill.name)
        self.stats.decline(skill.signature)
        return skill

    async def record_run(self, skill: Skill, ok: bool, error: str = "") -> Skill:
        entry = self.stats.record(skill.name, skill.version, ok, error)
        return replace(
            skill,
            runs=int(entry.get("runs", 0)),
            clean_runs=int(entry.get("clean", 0)),
            fails_in_row=int(entry.get("fails_in_row", 0)),
            last_error=str(entry.get("last_error", "")),
        )
