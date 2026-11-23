"""Forget the last stretch of time: everything she kept from the last 15, 30 or 60 minutes, or the last day, gone
from every store at once, after a full backup of what changes.

One general step over one map of where her records live (``STORES``): rows of her databases, lines of her logs and
record files, items of a few JSON files, and her brains' transcripts. Each kind of store says how to find what's
from the window (``at or after`` the cutoff); nothing is special-cased by store.

How it runs (``docs/forget.md``):

* The user picks it in her settings window (Privacy) or the tray, and confirms once. That is the yes: nothing asks
  again.
* While she runs, she can't cut her own open databases, and what she holds in mind (the conversation, her work log)
  would outlive the files. So ``request`` leaves ``data/forget.json`` with the cutoff, and she restarts into a new
  conversation. The next start runs it (``run_pending``) first thing, before anything opens a store or the log.
  When she isn't running, the settings window runs it directly.
* Before anything is deleted, every file that will change is copied whole to a dated folder outside her data
  (``~/octavia-backups``). If the backup fails, nothing is deleted.
* The window is fixed when it's asked for: from the cutoff to a few minutes after asking (``GRACE_S``), so what she
  writes on her way out goes too, and a start that comes late never takes what came after.

What it leaves on purpose: settings, sign-ins and tokens; what she counts of your Claude limits; eval runs and
night-shift scores; graded eval cases (only candidates found in the window go); background-task reports and
overnight work that you kept; the self-improvement journal.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# The choices the user is offered: minutes, and how she says it.
CHOICES: dict[int, str] = {15: "15 minutes", 30: "30 minutes", 60: "hour", 1440: "day"}
PENDING_FILE = "data/forget.json"
RESULT_FILE = "data/forget_result.json"
BACKUP_DIR = Path.home() / "octavia-backups"
# How long after it was asked a pending forget still reaches: her way out (the reply said, the restart) is inside the
# window. A restart that never came (she was shut down instead) runs at the next start, and still takes only this.
GRACE_S = 15 * 60
FOREVER = 4e9


def label(minutes: int) -> str:
    """'the last 30 minutes', 'the last hour', 'the last day'."""
    return f"the last {CHOICES.get(minutes, f'{minutes} minutes')}"


# ---------------------------------------------------------------- the kinds of store


@dataclass(frozen=True)
class Rows:
    """Rows of a SQLite table whose ``column`` (seconds since the epoch) is at or after the cutoff. ``first`` are
    statements run before the delete, with ``:cut`` bound: they undo what a row from the window did to older rows
    (a note it replaced is current again)."""

    path: str
    table: str
    column: str
    first: tuple[str, ...] = ()
    where: str = ""  # more conditions, ANDed (``status = 'candidate'``)


@dataclass(frozen=True)
class Lines:
    """Lines of a JSON-lines file whose ``key`` (epoch seconds) is at or after the cutoff."""

    path: str
    key: str = "ts"


@dataclass(frozen=True)
class Items:
    """Items of a list in a JSON file (``list_key`` of the top object, or the top list when empty) whose ``key`` is
    at or after the cutoff, and that match ``only`` when it's given."""

    path: str
    list_key: str
    key: str
    only: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class Log:
    """A text log whose lines start with a local time (``stamp``). A line without one belongs to the stamped line
    above it (a traceback). ``blocks``: the stamps only mark blocks (her output log: each start, and her revive
    task every two minutes while it runs): the block open at the cutoff may hold lines from after it, so it goes too,
    however long it is (it's in the backup)."""

    glob: str
    stamp: str
    blocks: bool = False


@dataclass(frozen=True)
class Transcripts:
    """Her brains' transcripts (the Claude CLI's, under her own claude-home): lines from the window go, and a
    transcript left with nothing from before goes whole, with the session's side folders. The CLI's own title and
    'last prompt' lines of a transcript that lost lines go too: they repeat what was said."""

    home: str = "data/claude-home"


_LOCAL = r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"

STORES: tuple[Rows | Lines | Items | Log | Transcripts, ...] = (
    # Her conversations and notes. A correction made in the window is undone: the note it replaced is current again.
    Rows("data/octavia.db", "memories", "created_at",
         first=("UPDATE memories SET superseded_by = NULL, superseded_at = NULL "
                "WHERE superseded_at >= :cut AND superseded_at < :until",)),
    Rows("data/octavia.db", "turns", "created_at"),
    # A conversation that ran into the window keeps its start, but not its summary of what was said.
    Rows("data/octavia.db", "sessions", "started_at",
         first=("UPDATE sessions SET summary = NULL WHERE started_at < :cut AND (ended_at IS NULL OR ended_at >= :cut)",)),
    # Her inner life.
    Rows("data/inner/inner.db", "entries", "created_at"),
    Rows("data/inner/inner.db", "thoughts", "created_at"),
    Rows("data/inner/inner.db", "experiences", "created_at"),
    Rows("data/inner/inner.db", "feelings", "created_at"),
    Rows("data/inner/inner.db", "opinions", "created_at",
         first=("UPDATE opinions SET replaced_by = NULL, active = 1 "
                "WHERE replaced_by IN (SELECT id FROM opinions WHERE created_at >= :cut AND created_at < :until)",)),
    Rows("data/inner/inner.db", "self_notes", "created_at",
         first=("UPDATE self_notes SET replaced_by = NULL, active = 1 "
                "WHERE replaced_by IN (SELECT id FROM self_notes WHERE created_at >= :cut AND created_at < :until)",)),
    Rows("data/inner/inner.db", "reflections", "started_at"),
    # Octavia Futures: what she saw happen, and what she made of it.
    Rows("data/futures/futures.db", "events", "ts"),
    Rows("data/futures/futures.db", "episodes", "started_at"),
    Rows("data/futures/futures.db", "lessons", "ts"),
    Rows("data/futures/futures.db", "recommendations", "created_at"),
    Rows("data/futures/futures.db", "outcomes", "started_at"),
    Rows("data/futures/futures.db", "performance", "ts"),
    Rows("data/futures/futures.db", "changes", "ts"),
    Rows("data/futures/futures.db", "entity_changes", "ts"),
    Rows("data/futures/futures.db", "entities", "first_seen"),
    Rows("data/futures/futures.db", "relations", "created_at"),
    Rows("data/futures/futures.db", "predictions", "created_at"),
    Rows("data/futures/futures.db", "hypothesis_evidence", "ts"),
    Rows("data/futures/futures.db", "hypotheses", "created_at"),
    Rows("data/futures/futures.db", "strategy_trials", "ts"),
    Rows("data/futures/futures.db", "strategies", "created_at"),
    Rows("data/futures/futures.db", "trials", "assigned_at"),
    Rows("data/futures/futures.db", "triggers", "created_at"),
    # Screen history.
    Rows("data/recall/screen_history.db", "snippets", "at"),
    Rows("data/recall/screen_history.db", "activity", "started"),
    # What happened on the computer, for her automations.
    Rows("data/automation/automation.db", "events", "ts"),
    Rows("data/automation/automation.db", "runs", "ts"),
    # Records of turns: what she claimed, how each was routed, what was heard and whether it was for her.
    Lines("data/claims.jsonl"),
    Lines("data/local_route.jsonl"),
    Lines("data/routing_log.jsonl"),
    Lines("data/local_shadow.jsonl"),
    Lines("data/addressee.jsonl"),
    Lines("data/screen_tasks.jsonl"),
    Lines("data/skills/repeats.jsonl", "at"),
    # What she said on her own, and failed requests not yet kept as eval cases.
    Items("data/unprompted/state.json", "said", "at"),
    Items("data/eval/caught.json", "cases", "found", only=(("status", "candidate"),)),
    Items("data/eval/replay/cases.json", "cases", "ts"),
    # What goes into each fresh prompt (documents she read, promises she made), what a restart would run again (a
    # background task asked for then), the panels the tray can bring back, and what her scheduled jobs said.
    Items("data/known_documents.json", "documents", "at"),
    Items("data/promises.json", "", "at"),
    Items("data/tasks/tasks.json", "", "created"),
    Items("data/visuals/shelf.json", "", "at"),
    Lines("data/scheduler/runs.jsonl", "at"),
    # Her logs.
    Log("data/logs/octavia.log*", _LOCAL),
    Log("data/logs/octavia-*.log", _LOCAL),
    Log("data/logs/octavia.crash.log", _LOCAL),
    Log("data/logs/*.stdio.log", r"^--- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", blocks=True),
    Transcripts(),
)  # fmt: skip


# ---------------------------------------------------------------- planning and applying


@dataclass
class Change:
    """One store's part: what it is, how many records go, and how to apply it."""

    what: str  # "data/octavia.db turns", "data/claims.jsonl"
    count: int
    files: tuple[Path, ...]  # copied before it's applied
    apply: Callable[[], None]
    touched: int = 0  # older rows it puts back (a note a correction from the window had replaced)


@dataclass
class Result:
    minutes: int
    cutoff: float
    ok: bool
    removed: dict[str, int] = field(default_factory=dict)
    backup: str = ""
    error: str = ""
    at: float = field(default_factory=time.time)

    @property
    def total(self) -> int:
        return sum(self.removed.values())


def plan(root: Path, cutoff: float, until: float = FOREVER, stores: Iterable[Any] = STORES) -> list[Change]:
    """What forgetting from ``cutoff`` to ``until`` would remove, store by store (read-only). Stores that aren't
    there, or whose table or column isn't (an older database), are left out."""
    root = Path(root)
    window = (cutoff, until)
    changes: list[Change] = []
    for store in stores:
        try:
            changes += _plan_one(root, window, store)
        except Exception as exc:  # one odd store never stops the rest: it's said, and it's left as it was
            log.warning("Forget: couldn't read %s: %s", _name(store), exc)
    return [c for c in changes if c.count or c.touched]


def _name(store: Any) -> str:
    return getattr(store, "path", None) or getattr(store, "glob", None) or getattr(store, "home", "?")


Window = tuple[float, float]


def _inside(when: float | None, window: Window) -> bool:
    return when is not None and window[0] <= when < window[1]


def _plan_one(root: Path, window: Window, store: Any) -> list[Change]:
    if isinstance(store, Rows):
        return _plan_rows(root, window, store)
    if isinstance(store, Lines):
        return _plan_lines(root, window, store)
    if isinstance(store, Items):
        return _plan_items(root, window, store)
    if isinstance(store, Log):
        return [c for path in sorted(root.glob(store.glob)) if path.is_file() for c in _plan_log(path, window, store)]
    if isinstance(store, Transcripts):
        return _plan_transcripts(root, window, store)
    raise TypeError(f"not a store: {store!r}")


def _tidy(path: Path) -> None:
    """After a database lost rows: its full-text indexes merged (a deleted row's words otherwise stay in old index
    segments) and its write-ahead log folded in and emptied."""
    con = sqlite3.connect(path, timeout=30)
    try:
        con.execute("PRAGMA secure_delete = ON")
        tables = [
            name for name, sql in con.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'")
            if sql and re.search(r"USING\s+fts[45]", sql, re.I)
        ]  # fmt: skip
        for name in tables:
            try:
                with con:
                    con.execute(f'INSERT INTO "{name}"("{name}") VALUES (\'optimize\')')
            except sqlite3.Error as exc:
                log.debug("Forget: couldn't merge %s's index %s: %s", path, name, exc)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()


def _sqlite_files(path: Path) -> tuple[Path, ...]:
    return tuple(p for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm")) if p.exists())


def _plan_rows(root: Path, window: Window, store: Rows) -> list[Change]:
    path = root / store.path
    if not path.exists():
        return []
    bound = {"cut": window[0], "until": window[1]}
    column = f'"{store.column}"'
    where = f"{column} >= :cut AND {column} < :until" + (f" AND ({store.where})" if store.where else "")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        try:
            count = con.execute(f'SELECT count(*) FROM "{store.table}" WHERE {where}', bound).fetchone()[0]
            touched = 0
            for stmt in store.first:  # what an UPDATE would touch, so a correction alone still counts
                target = re.search(r"WHERE (.*)$", stmt, re.S)
                table = re.match(r"UPDATE (\w+)", stmt)
                if target and table:
                    touched += con.execute(
                        f"SELECT count(*) FROM {table.group(1)} WHERE {target.group(1)}", bound
                    ).fetchone()[0]
        except sqlite3.OperationalError as exc:
            if "no such" in str(exc):
                return []
            raise
    finally:
        con.close()
    if not count and not touched:
        return []

    def apply() -> None:
        con = sqlite3.connect(path, timeout=30)
        try:
            con.execute("PRAGMA secure_delete = ON")  # the deleted text is overwritten, not left in free pages
            with con:
                for stmt in store.first:
                    con.execute(stmt, bound)
                con.execute(f'DELETE FROM "{store.table}" WHERE {where}', bound)
        finally:
            con.close()

    return [Change(f"{store.path} {store.table}", count, _sqlite_files(path), apply, touched)]


def _epoch(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value[:2] in ("19", "20"):
        try:
            when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (when if when.tzinfo else when.astimezone()).timestamp()
    return None


def _write_text(path: Path, text: str, *, in_place: bool = False) -> None:
    """``text`` as the file's whole content. Replaced whole through a temporary file, so a kill mid-write leaves the
    old file or the new one, never half; ``in_place`` (a log another process holds open to append, like her output
    log) is rewritten from the start and cut to length instead, where that process keeps writing."""
    data = text.encode("utf-8", "surrogateescape")
    if in_place:
        with open(path, "r+b") as f:
            f.write(data)
            f.truncate()
            f.flush()
            os.fsync(f.fileno())
        return
    tmp = path.with_name(f"{path.name}.forget.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(6):  # Windows refuses for a moment while another process reads it
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 5:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05 * (attempt + 1))


def _read_lines(path: Path) -> list[str]:
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.readlines()


def _plan_lines(root: Path, window: Window, store: Lines) -> list[Change]:
    path = root / store.path
    if not path.exists():
        return []
    kept: list[str] = []
    gone = 0
    for line in _read_lines(path):
        try:
            when = _epoch(json.loads(line).get(store.key))
        except (ValueError, AttributeError):
            when = None
        if _inside(when, window):
            gone += 1
        else:
            kept.append(line)
    if not gone:
        return []
    return [Change(store.path, gone, (path,), lambda: _write_text(path, "".join(kept)))]


def _plan_items(root: Path, window: Window, store: Items) -> list[Change]:
    path = root / store.path
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get(store.list_key) if store.list_key else data
    if not isinstance(items, list):
        return []

    def goes(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        return _inside(_epoch(item.get(store.key)), window) and all(item.get(k) == v for k, v in store.only)

    kept = [item for item in items if not goes(item)]
    if len(kept) == len(items):
        return []
    if store.list_key:
        data[store.list_key] = kept
    else:
        data = kept

    def apply() -> None:
        tmp = path.with_name(f"{path.name}.forget.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    return [Change(store.path, len(items) - len(kept), (path,), apply)]


def _plan_log(path: Path, window: Window, store: Log) -> list[Change]:
    stamp = re.compile(store.stamp)
    lines = _read_lines(path)
    # Each line's time: its own stamp, or the one above it.
    times: list[float | None] = []
    current: float | None = None
    starts: list[int] = []  # where each stamped block starts
    for i, line in enumerate(lines):
        m = stamp.match(line)
        if m:
            try:
                current = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()  # local time
            except ValueError:
                pass
            else:
                starts.append(i)
        times.append(current)
    drop = [_inside(t, window) for t in times]
    if store.blocks and any(drop):
        first = drop.index(True)
        before = [s for s in starts if s < first]
        if before:  # the block that was open at the cutoff
            for i in range(before[-1], first):
                drop[i] = True
    gone = sum(drop)
    if not gone:
        return []
    kept = "".join(line for line, d in zip(lines, drop, strict=True) if not d)
    rel = path.as_posix()
    return [
        Change(
            rel[rel.find("data/") :] if "data/" in rel else path.name,
            gone,
            (path,),
            lambda: _write_text(path, kept, in_place=True),
        )
    ]


def _sanitized(path: Path) -> str:
    """The Claude CLI's folder name for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


CONTENT_LINES = frozenset({"last-prompt", "ai-title", "summary"})


def _plan_transcripts(root: Path, window: Window, store: Transcripts) -> list[Change]:
    home = root / store.home
    projects = home / "projects"
    if not projects.is_dir():
        return []
    mine = _sanitized(root / "data")  # her own working folders (the brain, her work folders), not eval runs
    changes: list[Change] = []
    gone_total = 0
    files: list[Path] = []
    applies: list[Callable[[], None]] = []
    for folder in sorted(projects.iterdir()):
        if not folder.is_dir() or not folder.name.startswith(mine):
            continue
        for path in sorted(folder.glob("*.jsonl")):
            if path.stat().st_mtime < window[0]:
                continue  # nothing written since the cutoff
            plan_one = _plan_transcript(home, path, window)
            if plan_one is not None:
                gone, apply, side = plan_one
                gone_total += gone
                files += [path, *side]
                applies.append(apply)

    def apply_all() -> None:
        for apply in applies:
            apply()

    if gone_total:
        sessions = sum(1 for f in files if f.suffix == ".jsonl" and f.parent.parent == projects)
        changes.append(Change(f"{store.home} transcripts ({sessions})", gone_total, tuple(files), apply_all))
    return changes


def _plan_transcript(home: Path, path: Path, window: Window) -> tuple[int, Callable[[], None], list[Path]] | None:
    """What of one transcript goes: (lines, how, the side files and folders that change with it)."""
    lines = _read_lines(path)
    kept: list[str] = []
    gone = 0
    timed_kept = 0
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            kept.append(line)
            continue
        when = _epoch(d.get("timestamp")) if isinstance(d, dict) else None
        if when is not None:
            if _inside(when, window):
                gone += 1
                continue
            timed_kept += 1
        kept.append(line)
    folders = [path.with_suffix(""), home / "file-history" / path.stem, home / "session-env" / path.stem]
    if not timed_kept and gone:  # all of it is from the window: the whole session goes, side folders and all
        sides = [f for f in folders if f.is_dir()]

        def remove() -> None:
            path.unlink(missing_ok=True)
            for side in sides:
                shutil.rmtree(side, ignore_errors=True)

        return gone, remove, sides
    # A session that goes on: what its side folders got in the window (tool results, file copies) goes.
    late = [
        f for folder in folders if folder.is_dir() for f in folder.rglob("*")
        if f.is_file() and _inside(f.stat().st_mtime, window)
    ]  # fmt: skip
    if not gone and not late:
        return None

    def cut() -> None:
        if gone:  # the CLI's lines with what was said (its title, the last prompt) go: they may repeat the window
            _write_text(path, "".join(line for line in kept if _kind(line) not in CONTENT_LINES))
        for f in late:
            f.unlink(missing_ok=True)

    return gone + len(late), cut, late


def _kind(line: str) -> str:
    try:
        d = json.loads(line)
    except ValueError:
        return ""
    return str(d.get("type") or "") if isinstance(d, dict) else ""


# ---------------------------------------------------------------- running it


def backup(
    root: Path, changes: list[Change], minutes: int, *, to: Path | None = None, now: float | None = None
) -> Path:
    """Every file that will change, copied whole under a new dated folder (raises OSError when any copy fails)."""
    root = Path(root).resolve()
    stamp = datetime.fromtimestamp(now if now is not None else time.time()).strftime("%Y%m%d-%H%M%S")
    folder = (to or BACKUP_DIR) / f"{stamp}-forget-{minutes}min"
    n = 1
    while folder.exists():
        n += 1
        folder = folder.with_name(f"{stamp}-forget-{minutes}min-{n}")
    copied: set[Path] = set()
    for change in changes:
        for path in change.files:
            source = Path(path).resolve()
            if source in copied or not source.exists():
                continue
            rel = source.relative_to(root) if source.is_relative_to(root) else Path(source.name)
            target = folder / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target)
            copied.add(source)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "forget.json").write_text(
        json.dumps({"minutes": minutes, "files": sorted(str(p) for p in copied)}, indent=2), encoding="utf-8"
    )
    return folder


def forget(root: Path, minutes: int, cutoff: float, until: float = FOREVER, *, backup_to: Path | None = None) -> Result:
    """Forget everything from ``cutoff`` to ``until``: plan, back up, apply. Nothing is deleted when the backup
    fails."""
    root = Path(root)
    result = Result(minutes=minutes, cutoff=cutoff, ok=False)
    changes = plan(root, cutoff, until)
    if not changes:
        result.ok = True
        return result
    try:
        result.backup = str(backup(root, changes, minutes, to=backup_to))
    except OSError as exc:
        result.error = f"the backup failed ({exc}), so nothing was deleted"
        return result
    failed = []
    databases: dict[Path, None] = {}
    for change in changes:
        try:
            change.apply()
            result.removed[change.what] = change.count
        except Exception as exc:
            failed.append(change.what)
            log.warning("Forget: couldn't change %s: %s", change.what, exc)
        else:
            databases.update((Path(f), None) for f in change.files[:1] if str(f).endswith(".db"))
    for path in databases:
        try:
            _tidy(path)
        except sqlite3.Error as exc:  # the rows are gone either way; their words may stay in free space a while
            log.warning("Forget: couldn't tidy %s: %s", path, exc)
    result.ok = not failed
    if failed:
        result.error = f"{', '.join(failed)} couldn't be changed"
    return result


def request(root: Path, minutes: int, *, now: float | None = None) -> float:
    """Leave the forget for her next start (``run_pending``). Returns the cutoff."""
    if minutes not in CHOICES:
        raise ValueError(f"forget the last {minutes} minutes isn't one of the choices")
    now = time.time() if now is None else now
    cutoff = now - minutes * 60
    path = Path(root) / PENDING_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    asked_at = now
    try:  # one asked for already and not run yet: it's kept, with this one added
        earlier = json.loads(path.read_text(encoding="utf-8"))
        if float(earlier["cutoff"]) < cutoff:
            cutoff, minutes = float(earlier["cutoff"]), max(minutes, int(earlier["minutes"]))
        asked_at = max(now, float(earlier["asked_at"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps({"minutes": minutes, "cutoff": cutoff, "asked_at": asked_at}), encoding="utf-8")
    tmp.replace(path)
    return cutoff


def run_pending(root: Path, *, backup_to: Path | None = None) -> Result | None:
    """At her start, before anything opens a store: the forget she was asked for, if one waits. Its result is left
    for her to say (``take_result``). A pending forget that doesn't read is dropped, and said."""
    root = Path(root)
    path = root / PENDING_FILE
    if not path.exists():
        return None
    try:
        asked = json.loads(path.read_text(encoding="utf-8"))
        minutes, cutoff, asked_at = int(asked["minutes"]), float(asked["cutoff"]), float(asked["asked_at"])
    except (OSError, ValueError, KeyError, TypeError):
        result = Result(minutes=0, cutoff=0.0, ok=False, error="the request to forget didn't read")
    else:
        # However late it runs, it takes only the window and her way out (``GRACE_S``), never what came after.
        result = forget(root, minutes, cutoff, asked_at + GRACE_S, backup_to=backup_to)
    path.unlink(missing_ok=True)  # once, whatever came of it: a failing forget must not run at every start
    _save_result(root, result)
    return result


def _save_result(root: Path, result: Result) -> None:
    path = Path(root) / RESULT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")


def take_result(root: Path) -> Result | None:
    """The last forget's result, once (it's removed): what she says when she's back."""
    path = Path(root) / RESULT_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        result = Result(
            **{k: data[k] for k in ("minutes", "cutoff", "ok", "removed", "backup", "error", "at") if k in data}
        )
    except (OSError, ValueError, TypeError):
        result = None
    path.unlink(missing_ok=True)
    return result


def spoken(result: Result) -> str:
    """What she says when she's back."""
    what = label(result.minutes) if result.minutes else "that time"
    if result.ok:
        return f"I've forgotten {what}."
    if result.removed:
        return f"I forgot most of {what}, but {result.error}. A copy of what was there is in your backups folder."
    return f"I couldn't forget {what}: {result.error}."


def note(result: Result) -> str:
    """What her brain is told about it: only that, never what was there."""
    if result.ok or result.removed:
        return (
            f"The user had you forget {label(result.minutes)}: everything you kept from then was deleted, so you have "
            "no record of it. Don't guess at what happened then or ask about it; if they bring it up, they can tell "
            "you again."
        )
    return "The user asked you to forget a stretch of time, but it couldn't be done; nothing was deleted."


def describe(changes: list[Change]) -> str:
    """For ``octavia forget --dry-run``: one line a store."""
    if not changes:
        return "Nothing to forget in that time."
    width = max(len(c.what) for c in changes)
    return "\n".join(
        f"{c.what.ljust(width)}  {c.count}" + (f" (and {c.touched} older changed)" if c.touched else "")
        for c in changes
    )
