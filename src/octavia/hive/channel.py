"""The update feed: signed releases the maintainer publishes, which public installs check for and apply.

A feed is a folder (or the same files at an https address): ``channel.json``, its signature ``channel.json.sig``
and the release zips ``octavia release build`` made. ``channel.json`` lists the releases oldest first, each with its
zip's SHA-256, so the one signature covers every zip. An install:

1. reads ``channel.json`` and refuses it unless the signature checks out with ``hive.public_key``;
2. takes the newest release after its own (``VERSION``);
3. downloads the zip, refuses it unless its SHA-256 is the signed one;
4. writes its files over the install's, never touching what's the user's (``KEEP``), and removes the files the old
   release had that the new one doesn't (``data/hive/installed.json``), so a deleted module doesn't linger.

Only the public edition applies updates: the owner's install is a git checkout and gets the same changes as commits.
Publishing (``publish``) only writes to a local folder; putting that folder online is the maintainer's step.

Beside the releases the same signed file carries **items**: what the collective learned that isn't code (a lesson,
for now), one by one. Each has an id, a version, the measured effect it was accepted on (``hive/measure.py``) and a
status. An install takes the active ones in at its next look at the feed (``sync_items``), no update needed, and a
revoked one is gone from its prompt at the look after the maintainer revokes it: a lesson that turns out wrong
doesn't wait for a release, and doesn't need one to have arrived.

The feed can't be turned back. Every signing raises its ``serial`` and sets when it ``expires``; an install
remembers the highest serial it has seen (``data/hive/seen.json``) and refuses an older or an expired feed, so a
copy of last month's signed file can't bring back a release or a lesson that was withdrawn. And an update can be
undone: the files it replaces are kept (``data/hive/backup``) and ``rollback`` puts them back.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from octavia.hive import signing

CHANNEL = "channel.json"
SIGNATURE = "channel.json.sig"
VERSION_FILE = "VERSION"
INSTALLED = Path("data") / "hive" / "installed.json"
ITEMS = Path("data") / "hive" / "items.json"  # the feed's items as this install last saw them
SEEN = Path("data") / "hive" / "seen.json"  # the newest feed this install has trusted
BACKUP = Path("data") / "hive" / "backup"  # what the last update replaced
# A signed feed is good for this long; every signing (a release, an item, ``renew``) renews it. It's also the longest
# a withdrawn item can be kept alive by whoever serves the feed handing out the copy from before: after it, an
# install trusts the feed no more and takes the feed's items out of its prompt (``clear_items``).
EXPIRES_S = 60 * 86400.0
MAX_ITEM_CHARS = 300  # a lesson is a sentence or two: nothing longer goes into every install's prompt
ACTIVE, REVOKED = "active", "revoked"
# The user's own: never written or removed by an update.
KEEP = (
    "data/",
    "models/",
    ".venv/",
    ".env",
    "config/local.yaml",
    "personality/persona.md",
    "personality/vocabulary.yaml",
)


class UpdateError(RuntimeError):
    pass


class StaleFeed(UpdateError):
    """The feed is past its date: nothing in it is trusted any more, its items included."""


@dataclass(frozen=True)
class Release:
    version: str
    file: str
    sha256: str
    notes: str = ""
    published: float = 0.0
    passed: int | None = None  # shared eval cases it passed...
    total: int | None = None  # ...of this many

    @property
    def pass_rate(self) -> float | None:
        return self.passed / self.total if self.passed is not None and self.total else None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Release:
        shared = raw.get("shared_eval") or {}
        return cls(
            version=str(raw["version"]),
            file=str(raw["file"]),
            sha256=str(raw["sha256"]),
            notes=str(raw.get("notes", "")),
            published=float(raw.get("published", 0)),
            passed=shared.get("passed"),
            total=shared.get("total"),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": self.version,
            "file": self.file,
            "sha256": self.sha256,
            "notes": self.notes,
            "published": self.published,
        }
        if self.total is not None:
            out["shared_eval"] = {"passed": self.passed, "total": self.total}
        return out


@dataclass(frozen=True)
class Item:
    """One thing the collective learned, shared on its own: its text, what it was accepted on, and whether it stands."""

    id: str
    kind: str  # "lesson": a short general rule every install's prompt gets
    text: str
    about: str = ""
    version: int = 1  # raised when its text changes
    status: str = ACTIVE
    added: float = 0.0
    # What it was measured to do before it was shared (``hive/measure.py``): the case it fixes, how often that case
    # passed without it and with it, and the shared cases' total with it.
    effect: dict[str, Any] = field(default_factory=dict)
    reason: str = ""  # why it was revoked

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Item:
        effect = raw.get("effect")
        return cls(
            id=str(raw["id"]),
            kind=str(raw.get("kind", "lesson")),
            text=" ".join(str(raw["text"]).split()),
            about=str(raw.get("about", "")),
            version=int(raw.get("version", 1)),
            status=ACTIVE if raw.get("status", ACTIVE) == ACTIVE else REVOKED,  # anything else isn't in force
            added=float(raw.get("added", 0)),
            effect=dict(effect) if isinstance(effect, dict) else {},
            reason=str(raw.get("reason", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id, "kind": self.kind, "version": self.version, "status": self.status, "text": self.text,
            "about": self.about, "added": self.added, "effect": self.effect,
        }  # fmt: skip
        if self.reason:
            out["reason"] = self.reason
        return out


@dataclass
class Channel:
    releases: list[Release]
    items: list[Item] = field(default_factory=list)
    serial: int = 0  # raised by every signing; 0: a feed from before there were serials
    expires: float = 0.0  # 0: no end set (the same old feeds)


def _fetch(feed: str, name: str) -> bytes:
    if feed.startswith(("https://", "http://")):
        import httpx

        from octavia.net import tls_context

        if feed.startswith("http://") and not feed.startswith(("http://127.0.0.1", "http://localhost")):
            raise UpdateError("the feed must be https (or this PC)")
        r = httpx.get(f"{feed.rstrip('/')}/{name}", timeout=60, verify=tls_context(), follow_redirects=True)
        r.raise_for_status()
        return r.content
    path = Path(feed) / name
    if not path.is_file():
        raise UpdateError(f"{name} isn't in the feed ({feed})")
    return path.read_bytes()


def read_channel(feed: str, public_key: str) -> Channel:
    """The feed, once its signature checks out (UpdateError / SignatureError if not). Whether it's the newest this
    install has seen is ``check_fresh``'s to say."""
    if not feed or not public_key:
        raise UpdateError("no feed or no maintainer key is set (hive.feed, hive.public_key)")
    data = _fetch(feed, CHANNEL)
    signing.verify(public_key, data, _fetch(feed, SIGNATURE).decode("ascii", "replace"))
    try:
        raw = json.loads(data)
        return Channel(
            releases=[Release.from_dict(r) for r in raw.get("releases") or []],
            items=_read_items(raw.get("items")),
            serial=int(raw.get("serial", 0)),
            expires=float(raw.get("expires", 0)),
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise UpdateError(f"the feed's channel.json doesn't read ({exc})") from exc


def _read_items(rows: Any) -> list[Item]:
    """The feed's items that read; one that doesn't (a kind from a later version, a slip) is left out and never
    stands in the way of the releases. The last entry for an id is the one that counts."""
    items: dict[str, Item] = {}
    for row in rows if isinstance(rows, list) else ():
        try:
            item = Item.from_dict(row)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if item.id and len(item.text) <= MAX_ITEM_CHARS and (item.text or item.status == REVOKED):
            items[item.id] = item
    return list(items.values())


def read(feed: str, public_key: str) -> list[Release]:
    """The feed's releases, oldest first, once its signature checks out (UpdateError / SignatureError if not)."""
    return read_channel(feed, public_key).releases


def seen_serial(root: Path) -> int:
    try:
        return int(json.loads((root / SEEN).read_text(encoding="utf-8")).get("serial", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def check_fresh(root: Path, channel: Channel, now: float | None = None) -> None:
    """Refuses a feed older than one this install has already trusted, or past its date: a signature says who made
    it, not that it's the latest, and an old copy would bring back what was withdrawn. Raises UpdateError."""
    seen = seen_serial(root)
    if channel.serial < seen:
        raise UpdateError(
            f"the feed is older than the one I've already seen (its serial {channel.serial}, mine {seen}): not trusted"
        )
    if channel.expires and (now if now is not None else time.time()) > channel.expires:
        raise StaleFeed("the feed is past its date (nothing was published for too long): not trusted")


def read_trusted(root: Path, feed: str, public_key: str) -> Channel:
    """The feed, signed and fresh, and remembered as the newest seen. What every look at the feed goes through."""
    channel = read_channel(feed, public_key)
    check_fresh(root, channel)
    if channel.serial > seen_serial(root):
        path = root / SEEN
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"serial": channel.serial, "at": time.time()}), encoding="utf-8")
        tmp.replace(path)
    return channel


def clear_items(root: Path) -> bool:
    """The feed's items out of this install's prompt (its feed is no longer trusted); whether there were any."""
    path = root / ITEMS
    try:
        had = bool(json.loads(path.read_text(encoding="utf-8")).get("items"))
    except (OSError, ValueError, AttributeError):
        had = False
    with contextlib.suppress(OSError):
        path.unlink()
    return had


def sync_items(root: Path, channel: Channel) -> tuple[list[str], list[str]]:
    """This install's copy of the feed's items made to match ``channel`` (a trusted one). Returns the ids that are
    new or changed and the ids no longer in force (revoked, or gone from the feed)."""
    path = root / ITEMS
    if channel.serial <= 0:
        return [], []  # a feed from before there were items says nothing about them: what's here stays
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        before = {str(i["id"]): i for i in saved.get("items") or []}
        revoked_before = sorted(str(i) for i in saved.get("revoked") or [])
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        before, revoked_before = {}, None
    active = {i.id: i.to_dict() for i in channel.items if i.status == ACTIVE}
    revoked = sorted(i.id for i in channel.items if i.status == REVOKED)
    new = sorted(k for k, v in active.items() if before.get(k) != v)
    gone = sorted(k for k in before if k not in active)
    if new or gone or revoked != revoked_before:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        saved = {"serial": channel.serial, "items": list(active.values()), "revoked": revoked}
        tmp.write_text(json.dumps(saved, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    return new, gone


def current_version(root: Path) -> str:
    path = root / VERSION_FILE
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def newer(releases: list[Release], current: str) -> Release | None:
    """The newest release when it isn't ``current`` (the feed is append-only, so newest is last)."""
    if not releases or releases[-1].version == current:
        return None
    return releases[-1]


def _kept(rel: str) -> bool:
    return any(rel == k or (k.endswith("/") and rel.startswith(k)) for k in KEEP)


def apply(root: Path, feed: str, public_key: str, release: Release, *, public: bool) -> list[str]:
    """Writes ``release`` over the install at ``root``; returns the files changed. Raises UpdateError."""
    if not public:
        raise UpdateError("this is the owner's own install (a git checkout): it gets changes as commits")
    data = _fetch(feed, release.file)
    if hashlib.sha256(data).hexdigest() != release.sha256:
        raise UpdateError(f"{release.file} isn't the file the maintainer signed")
    staging = root / "data" / "hive" / "staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    zip_path = staging / "release.zip"
    zip_path.write_bytes(data)
    new_files: dict[str, bytes] = {}
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            parts = Path(info.filename).parts
            if len(parts) < 2 or any(p in ("..", "") for p in parts) or Path(info.filename).is_absolute():
                raise UpdateError(f"the release holds an odd path: {info.filename!r}")
            rel = Path(*parts[1:]).as_posix()  # under its top folder, octavia-<version>/
            if not _kept(rel):
                new_files[rel] = z.read(info)
    if "src/octavia/edition.py" not in new_files:
        raise UpdateError("the release doesn't look like Octavia (no src/octavia/edition.py)")
    installed_path = root / INSTALLED
    try:
        before = set(json.loads(installed_path.read_text(encoding="utf-8")).get("files") or [])
    except (OSError, ValueError):
        before = set()
    changed = []
    # What this update replaces is kept, so it can be undone (``rollback``): one update back, no further. A try at
    # this same update that stopped partway already kept the real originals: they're left as they are.
    backup = root / BACKUP
    earlier = _backup_manifest(root)
    if not (earlier and earlier.get("to") == release.version and current_version(root) != release.version):
        shutil.rmtree(backup, ignore_errors=True)
        kept: list[str] = []
        added: list[str] = []
        for rel in sorted({*new_files, *(before - set(new_files))}):
            target = root / rel
            if _kept(rel) or (rel in new_files and target.exists() and target.read_bytes() == new_files[rel]):
                continue
            if target.is_file():
                (backup / "files" / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(target, backup / "files" / rel)
                kept.append(rel)
            elif rel in new_files:
                added.append(rel)
        backup.mkdir(parents=True, exist_ok=True)
        (backup / "manifest.json").write_text(
            json.dumps({
                "from": current_version(root), "to": release.version, "at": time.time(), "replaced": kept,
                "added": added, "installed": sorted(before),
            }),
            encoding="utf-8",
        )  # fmt: skip
    # VERSION goes last: until every other file is in place the install still says it's on the old release, so an
    # update that stops partway is offered (and can be tried) again.
    for rel, content in sorted(new_files.items(), key=lambda item: (item[0] == VERSION_FILE, item[0])):
        target = root / rel
        if target.exists() and target.read_bytes() == content:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".update")
        tmp.write_bytes(content)
        tmp.replace(target)
        changed.append(rel)
    for rel in sorted(before - set(new_files)):
        if not _kept(rel) and (root / rel).is_file():
            (root / rel).unlink()
            changed.append(f"{rel} (removed)")
    installed_path.parent.mkdir(parents=True, exist_ok=True)
    installed_path.write_text(json.dumps({"version": release.version, "files": sorted(new_files)}), encoding="utf-8")
    shutil.rmtree(staging, ignore_errors=True)
    return changed


def _backup_manifest(root: Path) -> dict[str, Any] | None:
    try:
        manifest = json.loads((root / BACKUP / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = (
        isinstance(manifest, dict)
        and isinstance(manifest.get("replaced"), list)
        and isinstance(manifest.get("added"), list)
    )
    return manifest if ok else None


def rollback(root: Path, *, public: bool) -> str:
    """Undoes the last update: the files it replaced are put back and the ones it added are removed. Returns the
    version the install is back on. Raises UpdateError when there's nothing kept to go back to. The dependencies an
    update installed aren't uninstalled: the older code runs on them, as it would have after any newer install.

    VERSION is put back last, so an undo that stops partway (a file in use) can simply be run again."""
    if not public:
        raise UpdateError("this is the owner's own install (a git checkout): it goes back with git")
    backup = root / BACKUP
    manifest = _backup_manifest(root)
    if manifest is None:
        raise UpdateError("there's no update kept to undo")
    replaced, added = [str(r) for r in manifest["replaced"]], [str(r) for r in manifest["added"]]
    if current_version(root) != manifest.get("to"):
        raise UpdateError("what's kept isn't from the update this install is on: nothing undone")
    missing = [rel for rel in replaced if not (backup / "files" / rel).is_file()]
    if missing:
        raise UpdateError(f"the kept copy is missing {len(missing)} file(s): nothing undone")
    for rel in sorted(replaced, key=lambda r: (r == VERSION_FILE, r)):
        target = root / rel
        if _kept(rel) or ".." in Path(rel).parts or Path(rel).is_absolute():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".update")
        shutil.copyfile(backup / "files" / rel, tmp)
        tmp.replace(target)
    for rel in added:
        if not _kept(rel) and ".." not in Path(rel).parts and (root / rel).is_file():
            (root / rel).unlink()
    installed = root / INSTALLED
    installed.write_text(
        json.dumps({"version": manifest.get("from", ""), "files": manifest.get("installed") or []}), encoding="utf-8"
    )
    shutil.rmtree(backup, ignore_errors=True)
    return str(manifest.get("from", ""))


def sign_channel(feed_dir: Path, key_path: Path, raw: dict[str, Any]) -> None:
    """``raw`` written as the feed's channel.json and signed, a serial on and good for ``EXPIRES_S`` from now. The
    only way the maintainer's side writes the feed. The serial is never below the clock's seconds, so a feed folder
    made anew (the old one lost) still signs above every serial an install has seen."""
    raw["serial"] = max(int(raw.get("serial", 0)) + 1, int(time.time()))
    raw["expires"] = time.time() + EXPIRES_S
    data = json.dumps(raw, indent=2, ensure_ascii=False).encode("utf-8")
    feed_dir.mkdir(parents=True, exist_ok=True)
    (feed_dir / CHANNEL).write_bytes(data)
    (feed_dir / SIGNATURE).write_text(signing.sign(key_path, data), encoding="ascii")


def renew(feed_dir: Path, key_path: Path) -> None:
    """The maintainer's step when nothing new is published for a while: the feed signed again, good for another
    ``EXPIRES_S``. Raises UpdateError when there's no feed there."""
    if not (feed_dir / CHANNEL).exists():
        raise UpdateError(f"there's no feed in {feed_dir}")
    sign_channel(feed_dir, key_path, _raw_channel(feed_dir))


def _raw_channel(feed_dir: Path) -> dict[str, Any]:
    path = feed_dir / CHANNEL
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    raw.setdefault("releases", [])
    raw.setdefault("items", [])
    return raw


def put_item(feed_dir: Path, key_path: Path, item: Item) -> Item:
    """The maintainer's step: ``item`` added to the feed, or put in place of the one with its id (a version on),
    and the feed signed again. Raises UpdateError for a text too long to be a lesson."""
    text = " ".join(item.text.split())
    if not text or len(text) > MAX_ITEM_CHARS:
        raise UpdateError(f"a shared lesson is one or two sentences ({MAX_ITEM_CHARS} characters at most)")
    raw = _raw_channel(feed_dir)
    old = next((i for i in raw["items"] if i.get("id") == item.id), None)
    version = int(old.get("version", 1)) + 1 if old is not None else 1
    item = Item(item.id, item.kind, text, item.about[:80], version, ACTIVE, item.added or time.time(), item.effect)
    raw["items"] = [i for i in raw["items"] if i.get("id") != item.id] + [item.to_dict()]
    sign_channel(feed_dir, key_path, raw)
    return item


def revoke_item(feed_dir: Path, key_path: Path, item_id: str, reason: str, *, shipped: bool = False) -> None:
    """The maintainer's step: the item stays in the feed, marked revoked with why, so every install drops it at its
    next look (an item only removed would linger where a look failed quietly). ``shipped``: the id is a lesson a
    release shipped (``shared_lessons.yaml``), not one of the feed's: it gets an entry here that only says revoked.
    Raises UpdateError for an id the feed doesn't have otherwise."""
    raw = _raw_channel(feed_dir)
    row = next((i for i in raw["items"] if i.get("id") == item_id), None)
    if row is None:
        if not shipped:
            raise UpdateError(f"there's no item {item_id!r} in the feed (a lesson a release shipped: --shipped)")
        row = {"id": item_id, "kind": "lesson", "version": 1, "text": "", "about": "", "added": time.time()}
        raw["items"].append(row)
    row["status"], row["reason"] = REVOKED, " ".join(reason.split())[:300]
    sign_channel(feed_dir, key_path, raw)


def zip_version(archive: Path) -> str:
    """The release's own version (its VERSION file, as installs compare it). Raises UpdateError without one."""
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            if name.count("/") == 1 and name.endswith("/" + VERSION_FILE):
                return z.read(name).decode("utf-8").strip()
    raise UpdateError(f"{archive.name} has no VERSION file: build it with `octavia release build`")


def publish(
    feed_dir: Path,
    archive: Path,
    key_path: Path,
    version: str,
    notes: str = "",
    passed: int | None = None,
    total: int | None = None,
) -> Release:
    """The maintainer's step: ``archive`` added to the feed folder and the channel signed again. Local only."""
    feed_dir.mkdir(parents=True, exist_ok=True)
    raw = _raw_channel(feed_dir)
    if any(r.get("version") == version for r in raw["releases"]):
        raise UpdateError(f"{version} is already in the feed")
    target = feed_dir / archive.name
    shutil.copyfile(archive, target)
    release = Release(
        version=version,
        file=archive.name,
        sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        notes=notes,
        published=time.time(),
        passed=passed,
        total=total,
    )
    raw["releases"].append(release.to_dict())
    sign_channel(feed_dir, key_path, raw)
    return release


def status(releases: list[Release], current: str) -> str:
    """Each release, its shared-eval pass rate and which one this install runs: the collective's progress."""
    if not releases:
        return "The feed has no releases yet."
    lines = []
    for r in releases:
        rate = f"{r.passed}/{r.total} shared cases ({r.pass_rate:.0%})" if r.pass_rate is not None else "not measured"
        mark = "  <- this install" if r.version == current else ""
        lines.append(f"{r.version}: {rate}{mark}")
    return "\n".join(lines)


def items_status(items: list[Item], dropped: set[str] = frozenset()) -> str:
    """Each shared item, what it was accepted on, and whether it's in force here."""
    if not items:
        return "Nothing has been shared on its own yet."
    lines = []
    for i in items:
        effect = i.effect or {}
        proof = (
            f"its case {effect.get('case', '?')} passed {effect.get('without', '?')} without it, "
            f"{effect.get('with', '?')} with it"
            if effect
            else "not measured"
        )
        state = f"revoked ({i.reason})" if i.status == REVOKED else ("dropped here" if i.id in dropped else "in force")
        lines.append(f"{i.id} v{i.version} [{state}] {i.text} ({proof})")
    return "\n".join(lines)
