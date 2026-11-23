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
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from octavia.hive import signing

CHANNEL = "channel.json"
SIGNATURE = "channel.json.sig"
VERSION_FILE = "VERSION"
INSTALLED = Path("data") / "hive" / "installed.json"
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


def read(feed: str, public_key: str) -> list[Release]:
    """The feed's releases, oldest first, once its signature checks out (UpdateError / SignatureError if not)."""
    if not feed or not public_key:
        raise UpdateError("no feed or no maintainer key is set (hive.feed, hive.public_key)")
    data = _fetch(feed, CHANNEL)
    signing.verify(public_key, data, _fetch(feed, SIGNATURE).decode("ascii", "replace"))
    try:
        raw = json.loads(data)
        return [Release.from_dict(r) for r in raw.get("releases") or []]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise UpdateError(f"the feed's channel.json doesn't read ({exc})") from exc


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
    for rel, content in sorted(new_files.items()):
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
    channel_path = feed_dir / CHANNEL
    raw = json.loads(channel_path.read_text(encoding="utf-8")) if channel_path.exists() else {"releases": []}
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
    data = json.dumps(raw, indent=2).encode("utf-8")
    channel_path.write_bytes(data)
    (feed_dir / SIGNATURE).write_text(signing.sign(key_path, data), encoding="ascii")
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
