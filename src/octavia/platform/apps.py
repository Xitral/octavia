"""Index of installed applications, and the only way the assistant launches one.

Two sources, both read-only:

* the Start menu's ``shell:AppsFolder`` (what ``Get-StartApps`` lists: store
  and desktop apps, with stable app IDs that survive updates), read in-process
  through Shell.Application COM;
* the ``App Paths`` registry key, which finds apps without a Start-menu entry
  (Chrome, for example).

Spoken names are matched in tiers, most precise first, and fuzzy matching
only wins with a high score *and* a clear margin, because plain fuzzy
matching confidently opens the wrong app ("photoshop" scores 90 against
"Photos"). When unsure, ``resolve`` returns candidate names so the model can
ask the user.

``launch`` only accepts entries from this index or URIs whose scheme is on an
allow-list. It never takes paths, command lines or arguments.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from rapidfuzz import fuzz, process

log = logging.getLogger(__name__)

DEFAULT_URI_PREFIXES = ("ms-settings:", "spotify:", "discord:", "calculator:", "vscode:", "ms-clock:")

CACHE_VERSION = 1
FUZZY_MIN_SCORE = 92.0
FUZZY_MIN_MARGIN = 8.0

# Start-menu entries that run scripts or installers are not "apps" to open by voice.
_BLOCKED_EXTENSIONS = {
    ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta",
    ".msi", ".msp", ".reg", ".scr", ".pif", ".jar", ".py", ".pyw",
}  # fmt: skip
_UNINSTALL = re.compile(r"uninst", re.IGNORECASE)
_DUPLICATE_SUFFIX = re.compile(r"\s*\(\d+\)$")  # "File Explorer (2)" is a pinned-folder duplicate
_FILLER_WORDS = re.compile(r"\b(the|app|apps|application|program)\b")
_URI_LIKE = re.compile(r"^[a-z][a-z0-9+.\-]*:\S*$", re.IGNORECASE)
_URI_BAD_CHARS = re.compile(r'[\x00-\x20\x7f"<>\\^`{|}]')
_PATH_LIKE = re.compile(r"^[a-z]:|[\\/]|\.(exe|com|bat|cmd|lnk|msc|ps1)$", re.IGNORECASE)


@dataclass(frozen=True)
class AppEntry:
    name: str  # display name, e.g. "Visual Studio Code"
    target: str  # AppsFolder app ID, exe path (App Paths) or URI
    kind: str  # "appsfolder" | "apppaths" | "uri"


class LaunchRefused(ValueError):
    """The target is not something the assistant is allowed to start."""


def normalize(text: str) -> str:
    """Lowercase, drop filler words and punctuation: "The Sims™ 4" -> "sims 4"."""
    s = text.casefold().replace("&", " and ").replace("'", "").replace("’", "")
    s = _FILLER_WORDS.sub(" ", s)
    s = re.sub(r"[^\w+#]+|_", " ", s)
    return " ".join(s.split())


def looks_like_path(text: str) -> bool:
    """True for things that are files or commands rather than app names."""
    return bool(_PATH_LIKE.search(text.strip()))


def looks_like_uri(text: str) -> bool:
    """True for "scheme:rest" strings such as ``ms-settings:display`` or ``https://...``."""
    text = text.strip()
    return bool(_URI_LIKE.match(text)) and not re.match(r"^[a-z]:", text, re.IGNORECASE)


def _normalize_prefix(prefix: str) -> str | None:
    p = prefix.strip().lower()
    if not p:
        return None
    if ":" not in p:
        p += ":"
    scheme = p.split(":", 1)[0]
    if not re.fullmatch(r"[a-z][a-z0-9+.\-]*", scheme) or scheme in {"file", "javascript", "vbscript", "data"}:
        log.warning("Ignoring URI prefix %r: not an allowed scheme", prefix)
        return None
    if len(scheme) == 1:  # "c:" is a drive letter, not a scheme
        return None
    return p


@contextmanager
def _com_apartment() -> Iterator[None]:
    """Initialise COM on this (worker) thread for the duration of the block.

    A thread already in another apartment keeps it: COM still works there, it
    just must not be uninitialised by us.
    """
    try:
        import pythoncom
    except ImportError:  # pragma: no cover - pywin32 is a Windows dependency
        yield
        return
    try:
        pythoncom.CoInitialize()
        initialised = True
    except pythoncom.com_error:
        initialised = False
    try:
        yield
    finally:
        if initialised:
            pythoncom.CoUninitialize()


def _read_appsfolder() -> list[AppEntry]:
    import win32com.client

    folder = win32com.client.Dispatch("Shell.Application").NameSpace("shell:AppsFolder")
    entries = []
    for item in folder.Items():
        name, target = str(item.Name or "").strip(), str(item.Path or "").strip()
        if name and target:
            entries.append(AppEntry(name, target, "appsfolder"))
    return entries  # COM objects are released here, before the apartment is torn down


def _enum_appsfolder() -> list[AppEntry]:
    """Start-menu apps via Shell.Application (same set as Get-StartApps, ~0.8 s, in-process)."""
    if sys.platform != "win32":
        return []
    with _com_apartment():
        return _read_appsfolder()


def _enum_app_paths() -> list[AppEntry]:
    """Executables registered under App Paths (HKCU and 64-bit HKLM)."""
    if sys.platform != "win32":
        return []
    import winreg

    out: list[AppEntry] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            root = winreg.OpenKey(
                hive,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            )
        except OSError:
            continue
        with root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(root, sub) as key:
                        value = winreg.QueryValueEx(key, "")[0]
                except OSError:
                    continue
                exe = os.path.expandvars(str(value).strip().strip('"'))
                if exe.lower().endswith(".exe") and os.path.isfile(exe):
                    out.append(AppEntry(Path(sub).stem, exe, "apppaths"))  # "chrome.exe" -> "chrome"
    return out


def _is_launchable(entry: AppEntry) -> bool:
    if _UNINSTALL.search(entry.name) or _UNINSTALL.search(entry.target):
        return False
    target = entry.target
    if "://" in target:  # web links and game-store URIs from Start-menu shortcuts
        return True
    return os.path.splitext(target)[1].lower() not in _BLOCKED_EXTENSIONS


def _dedupe(raw: list[AppEntry]) -> list[AppEntry]:
    names = {e.name for e in raw}
    seen: set[tuple[str, str]] = set()
    entries: list[AppEntry] = []
    for e in raw:
        base = _DUPLICATE_SUFFIX.sub("", e.name)
        if base != e.name and base in names:
            continue
        if e.kind == "appsfolder" and os.path.isabs(e.target) and os.path.isdir(e.target):
            continue  # Start-menu entries that are just folders
        if not _is_launchable(e):
            continue
        key = (normalize(e.name), e.kind)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        entries.append(e)
    return entries


def _startfile(target: str, cwd: str | None = None) -> None:
    """Hand a target to the Windows shell (the same as double-clicking it)."""
    with _com_apartment():  # ShellExecute may load COM shell extensions
        if cwd:
            os.startfile(target, cwd=cwd)  # type: ignore[attr-defined]
        else:
            os.startfile(target)  # type: ignore[attr-defined]


class AppIndex:
    """Cached, matchable list of installed apps.

    ``load`` and ``launch`` block (COM, registry, ShellExecute): call them via
    ``asyncio.to_thread``. ``resolve`` is pure and takes a few milliseconds.
    """

    def __init__(
        self,
        cache_file: Path | None = None,
        aliases: dict[str, str] | None = None,
        *,
        ttl_s: float = 6 * 3600,
        extra_uri_prefixes: Iterable[str] = (),
        entries: Iterable[AppEntry] | None = None,
    ) -> None:
        self.cache_file = cache_file
        self.ttl_s = ttl_s
        self.aliases = {normalize(k): v.strip() for k, v in (aliases or {}).items() if normalize(k) and v.strip()}
        prefixes = [_normalize_prefix(p) for p in (*DEFAULT_URI_PREFIXES, *extra_uri_prefixes)]
        self.uri_prefixes: tuple[str, ...] = tuple(dict.fromkeys(p for p in prefixes if p))
        self.entries: list[AppEntry] = []
        self._by_norm: dict[str, AppEntry] = {}
        self._known: set[AppEntry] = set()
        self.built_at = 0.0
        if entries is not None:
            self._set_entries(list(entries), time.time())

    # ------------------------------------------------------------------ loading

    @property
    def loaded(self) -> bool:
        return self.built_at > 0

    @property
    def age_s(self) -> float:
        return time.time() - self.built_at if self.loaded else float("inf")

    def is_stale(self) -> bool:
        return self.age_s > self.ttl_s

    def _set_entries(self, entries: list[AppEntry], built_at: float) -> None:
        self.entries = entries
        self.built_at = built_at
        by_norm: dict[str, AppEntry] = {}
        for e in entries:  # first wins: Start-menu entries come before App Paths
            by_norm.setdefault(normalize(e.name), e)
        self._by_norm = by_norm
        self._known = set(entries)

    def load(self, force: bool = False) -> None:
        """Use the in-memory or cached index while fresh, otherwise rescan and rewrite the cache."""
        if not force and self.loaded and not self.is_stale():
            return
        if not force and self._load_cache():
            return
        self.rebuild()

    def rebuild(self) -> None:
        started = time.perf_counter()
        raw: list[AppEntry] = []
        for source in (_enum_appsfolder, _enum_app_paths):
            try:
                raw.extend(source())
            except Exception:
                log.exception("App index source %s failed", source.__name__)
        self._set_entries(_dedupe(raw), time.time())
        log.info("Indexed %d apps in %.2fs", len(self.entries), time.perf_counter() - started)
        self._save_cache()

    def _load_cache(self) -> bool:
        if self.cache_file is None or not self.cache_file.exists():
            return False
        try:
            data = json.loads(self.cache_file.read_text("utf-8"))
            if data.get("version") != CACHE_VERSION:
                return False
            built_at = float(data["built_at"])
            if not 0 <= time.time() - built_at <= self.ttl_s:
                return False
            entries = [AppEntry(**e) for e in data["entries"]]
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            log.warning("Ignoring unreadable app index cache %s", self.cache_file)
            return False
        # The cache is only a speed-up: apply the same rules as a fresh scan.
        valid = [e for e in entries if e.kind in ("appsfolder", "apppaths") and _is_launchable(e)]
        self._set_entries(valid, built_at)
        return True

    def _save_cache(self) -> None:
        if self.cache_file is None:
            return
        payload = {"version": CACHE_VERSION, "built_at": self.built_at, "entries": [asdict(e) for e in self.entries]}
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(self.cache_file.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
            os.replace(tmp, self.cache_file)
        except OSError:
            log.warning("Could not write app index cache %s", self.cache_file, exc_info=True)

    # ------------------------------------------------------------------ matching

    def is_allowed_uri(self, uri: str) -> bool:
        uri = uri.strip()
        if not uri or len(uri) > 512 or _URI_BAD_CHARS.search(uri):
            return False
        return uri.lower().startswith(self.uri_prefixes)

    def resolve(self, spoken: str) -> tuple[AppEntry | None, list[str]]:
        """``(entry, [])`` when confident, else ``(None, candidate_names)`` for the model to ask about."""
        raw = spoken.strip()
        if raw.lower().endswith(".exe") and not re.search(r"[\\/:]", raw):
            raw = raw[:-4]  # "notepad.exe" names an app; it can still only match the index
        q = normalize(raw)
        if not q:
            return None, []

        alias = self.aliases.get(q)
        if alias is not None:
            if self.is_allowed_uri(alias):
                return AppEntry(raw, alias, "uri"), []
            if _URI_LIKE.match(alias) or looks_like_path(alias):
                log.warning("App alias %r -> %r is not an allowed URI; ignoring it", raw, alias)
                return None, []
            q = normalize(alias)
        elif _URI_LIKE.match(raw):
            return (AppEntry(raw, raw, "uri"), []) if self.is_allowed_uri(raw) else (None, [])
        elif looks_like_path(raw):
            return None, []

        by_norm = self._by_norm
        if q in by_norm:
            return by_norm[q], []

        words = set(q.split())
        word_hits = [n for n in by_norm if words <= set(n.split())]  # "explorer" -> "file explorer"
        if len(word_hits) == 1:
            return by_norm[word_hits[0]], []
        if not word_hits:
            reverse = [n for n in by_norm if len(n) >= 4 and set(n.split()) <= words]  # "google chrome" -> "chrome"
            if len(reverse) == 1:
                return by_norm[reverse[0]], []

        top = process.extract(q, list(by_norm), scorer=fuzz.WRatio, processor=None, limit=5)
        if top and top[0][1] >= FUZZY_MIN_SCORE and (len(top) == 1 or top[0][1] - top[1][1] >= FUZZY_MIN_MARGIN):
            return by_norm[top[0][0]], []
        candidates = word_hits[:5] or [n for n, score, _ in top if score >= 60]
        return None, list(dict.fromkeys(by_norm[n].name for n in candidates))

    # ------------------------------------------------------------------ launching

    def launch_target(self, entry: AppEntry) -> tuple[str, str | None]:
        """The exact shell target (and working directory) ``launch`` would use, or LaunchRefused."""
        if entry.kind == "uri":
            if not self.is_allowed_uri(entry.target):
                raise LaunchRefused(f"URI {entry.target!r} is not on the allow-list")
            return entry.target.strip(), None
        if entry not in self._known:
            raise LaunchRefused(f"{entry.name!r} is not in the app index")
        if entry.kind == "appsfolder":
            return "shell:AppsFolder\\" + entry.target, None
        if entry.kind == "apppaths":
            if not entry.target.lower().endswith(".exe") or not os.path.isfile(entry.target):
                raise LaunchRefused(f"{entry.target!r} is no longer installed")
            return entry.target, os.path.dirname(entry.target) or None
        raise LaunchRefused(f"unknown entry kind {entry.kind!r}")

    def launch(self, entry: AppEntry) -> str:
        """Start an indexed app or allow-listed URI. Returns the target handed to the shell."""
        target, cwd = self.launch_target(entry)
        log.info("Launching %s -> %s", entry.name, target)
        _startfile(target, cwd)
        return target
