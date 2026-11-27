"""Every key Octavia reads from the environment (in practice, the ``.env`` file in her folder), in one list.

Each integration used to keep its own variable name, its own way of tidying a pasted value and its own "not
connected yet" wording, and ``.env.example`` knew about three of the eleven keys. Now this list is the one place: the
tools read their keys through ``value``, say how to connect through ``hint``/``missing``, ``octavia env`` and the
doctor check them all, and ``.env.example`` is ``example()`` (a test keeps the file in step).

Secrets never go in YAML, a tool result or a log line: everything here quotes key names and file paths only, never
a value. Every key is read through ``config.current_env``, which follows edits to the file; ``restart`` marks the
ones whose integration is set up at startup, so connecting it takes a restart.
"""

from __future__ import annotations

import difflib
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from octavia import config

if TYPE_CHECKING:
    from octavia.config import AppConfig


def _token(raw: str) -> str:
    """A pasted token or key: surrounding spaces, quotes and angle brackets dropped."""
    return raw.strip().strip("<>\"'").strip()


def _spaced(raw: str) -> str:
    """A password Google shows in groups of four: the spaces inside dropped too."""
    return "".join(_token(raw).split())


def _text(raw: str) -> str:
    """A value the integration tidies itself (several calendar addresses): only the ends trimmed."""
    return raw.strip()


@dataclass(frozen=True)
class Key:
    name: str
    service: str  # what it connects, as the user knows it: "TickTick", "Gmail"
    about: str  # what it's for, one line in .env.example
    get: str  # how the user gets one, finishing "To connect it, the user ..."
    placeholder: str = "<token>"
    noun: str = "value"  # what goes after NAME= ("address", "token"): "the line has no <noun> after it"
    restart: bool = False  # connecting it needs a restart (it's read, or what it connects is set up, at startup)
    clean: Callable[[str], str] = _token
    section: str = "Integrations"  # its heading in .env.example
    optional: bool = True  # False: Octavia can't talk without it
    commented: bool = False  # in .env.example as a commented-out line: only for a setup that needs it


KEYS: tuple[Key, ...] = (
    Key(
        "FISH_AUDIO_API_KEY",
        "Fish Audio",
        "Her voice (Fish Audio). Required.",
        "creates an API key at https://fish.audio/app/api-keys",
        placeholder="<key>",
        noun="key",
        restart=True,
        section="Voice",
        optional=False,
    ),
    Key(
        "FISH_AUDIO_VOICE_ID",
        "Fish Audio",
        "Optional: which voice she speaks with (32 hex characters), from https://fish.audio/app/discovery or your own "
        "cloned voice. tts.fish_audio.reference_id in config/local.yaml wins over it.",
        "picks a voice at https://fish.audio/app/discovery and copies its id",
        placeholder="<voice id>",
        noun="voice id",
        restart=True,
        section="Voice",
    ),
    Key(
        "ANTHROPIC_API_KEY",
        "Claude",
        "Only with brain.claude.auth: api_key in config/local.yaml. Otherwise leave it out: it would bill the API "
        "instead of using your Claude subscription (sign in once with: uv run octavia login).",
        "creates an API key at https://console.anthropic.com",
        placeholder="<key>",
        noun="key",
        restart=True,
        section="Brain",
        commented=True,
    ),
    Key(
        "OPENAI_API_KEY",
        "Another model",
        "Only with brain.provider: openai_compatible: the key of that model's server (OpenAI, or any server speaking "
        "its API). brain.openai_compatible.api_key_env can name another variable instead (GEMINI_API_KEY, ...).",
        "creates an API key on that model's site (OpenAI: https://platform.openai.com/api-keys)",
        placeholder="<key>",
        noun="key",
        restart=True,
        section="Brain",
        commented=True,
    ),
    Key(
        "TICKTICK_API_TOKEN",
        "TickTick",
        "TickTick (tasks, lists, habits): TickTick on the web, your avatar, Settings, Account & Security, API Token.",
        "makes an API token in TickTick (on the web: their avatar, Settings, Account & Security, API Token)",
        noun="token",
        restart=True,  # her TickTick tools are loaded at startup
    ),
    Key(
        "CANVAS_API_TOKEN",
        "Canvas",
        "Canvas (coursework, due dates, grades): in Canvas, Account, Settings, '+ New Access Token'.",
        "signs in to Canvas (their school's site, CANVAS_BASE_URL), "
        "opens Account, Settings, and clicks '+ New Access Token'",
        noun="token",
    ),
    Key(
        "CANVAS_BASE_URL",
        "Canvas",
        "Your school's Canvas address, e.g. https://yourschool.instructure.com (needed with the token).",
        "copies their school's Canvas address from the browser",
        placeholder="https://<school>.instructure.com",
        noun="address",
    ),
    Key(
        "GMAIL_ADDRESS",
        "Gmail",
        "Gmail (new-mail alerts, reading the inbox): your Gmail address...",
        "turns on 2-Step Verification in their Google account and makes an app password at "
        "myaccount.google.com/apppasswords",
        placeholder="<their Gmail address>",
        noun="address",
    ),
    Key(
        "GMAIL_APP_PASSWORD",
        "Gmail",
        "...and an app password for it (myaccount.google.com/apppasswords, needs 2-Step Verification); never your "
        "usual password. Only used to read the inbox.",
        "turns on 2-Step Verification in their Google account and makes an app password at "
        "myaccount.google.com/apppasswords",
        placeholder="<the 16-letter app password>",
        noun="app password",
        clean=_spaced,
    ),
    Key(
        "CALENDAR_ICS_URLS",
        "Calendar",
        "Your calendars' private iCal addresses, all on this one line, separated by spaces (Google Calendar: "
        "Settings, the calendar's name, 'Secret address in iCal format').",
        "copies a calendar's private iCal address (in Google Calendar it's under Settings, then the calendar's name, "
        "then 'Secret address in iCal format')",
        placeholder="<private iCal address> <another one>",
        noun="address",
        clean=_text,
    ),
    Key(
        "SPOTIFY_CLIENT_ID",
        "Spotify",
        "Reading your Spotify playlists: a free app at developer.spotify.com/dashboard with the Web API and the "
        "redirect URI http://127.0.0.1:8898/callback. Then ask her to connect Spotify.",
        "creates a free app at developer.spotify.com/dashboard with the Web API and the redirect URI "
        "http://127.0.0.1:8898/callback",
        placeholder="<client ID>",
        noun="client ID",
    ),
    Key(
        "AUDD_API_TOKEN",
        "Song recognition",
        "Naming the song that's playing: a free AudD token from dashboard.audd.io.",
        "signs up for a free AudD API token at dashboard.audd.io",
        noun="token",
    ),
    Key(
        "DISCORD_BOT_TOKEN",
        "The Discord bridge",
        "Texting her from your phone: your own Discord bot's token (discord.com/developers/applications, your app, "
        "Bot, Reset Token), with discord_bridge.enabled: true in config/local.yaml. See docs/discord-bridge.md.",
        "creates an application at https://discord.com/developers/applications, adds a Bot, turns on its Message "
        "Content intent and copies its token (Bot, Reset Token; docs/discord-bridge.md)",
        noun="token",
        restart=True,  # the bridge connects at startup
    ),
    Key(
        "DISCORD_OWNER_ID",
        "The Discord bridge",
        "...and your own numeric Discord user ID (Developer Mode on, right-click yourself, Copy User ID), unless "
        "discord_bridge.owner_id says it. Only that account is ever answered.",
        "turns on Developer Mode in Discord (Settings, Advanced), right-clicks their own name and picks Copy User ID",
        placeholder="<your user ID>",
        noun="user ID",
        restart=True,
    ),
    Key(
        "CESIUM_ION_TOKEN",
        "God's Eye View",
        "Optional: photorealistic 3D cities and terrain in God's Eye View, a free Cesium ion token (ion.cesium.com, "
        "Access Tokens). Without one it shows flat satellite imagery.",
        "signs up free at ion.cesium.com and copies a token from Access Tokens",
        noun="token",
        section="Maps",
    ),
    Key(
        "GOOGLE_MAPS_API_KEY",
        "God's Eye View",
        "Optional: Google's photorealistic 3D tiles and place search in God's Eye View (console.cloud.google.com, "
        "Map Tiles API; billing required, so set a budget).",
        "creates a key with the Map Tiles API in console.cloud.google.com (billing required)",
        placeholder="<key>",
        noun="key",
        section="Maps",
    ),
    Key(
        "OCTAVIA_CONFIG",
        "Settings",
        "Another settings file to use instead of config/local.yaml.",
        "names the settings file",
        placeholder="<path>",
        noun="path",
        restart=True,
        clean=_text,
        section="Advanced",
        commented=True,
    ),
    Key(
        "OCTAVIA_MODELS_DIR",
        "Settings",
        "Where the speech models live, if not in models/.",
        "names the folder",
        placeholder="<folder>",
        noun="folder",
        clean=_text,
        section="Advanced",
        commented=True,
    ),
)
_BY_NAME = {key.name: key for key in KEYS}
# Environment variables Octavia sets or reads for herself: never the user's, so never "unknown" in their .env.
INTERNAL = frozenset(
    {
        "OCTAVIA_HOME",
        "OCTAVIA_SUPERVISED",
        "OCTAVIA_SUPERVISOR_PID",
        "OCTAVIA_USAGE_DIR",
        "ANTHROPIC_AUTH_TOKEN",
        "OCTAVIA_USER_DIR",
    }
    | {"ETHER_TRIAL_LESSON"}  # a lesson being measured before it's shared (``hive/measure.py``), its own process only
    | {"OCTAVIA_EVAL_JUDGE"}  # the eval's judge pick (``evolve/evals.py`` --judge), set for its own runs
)
# Words too common in key names to say two keys are the same thing.
_GENERIC = frozenset({"API", "KEY", "TOKEN", "URL", "URLS", "ID", "BASE", "APP", "PASSWORD", "ADDRESS"})


def key(name: str) -> Key:
    return _BY_NAME[name]


def value(name: str) -> str:
    """The key's value, tidied the way that key is ("" when it isn't set). Follows edits to ``.env``."""
    return key(name).clean(config.current_env(name) or "")


def is_set(name: str) -> bool:
    return bool(value(name))


def env_file() -> Path | None:
    """The ``.env`` file she reads (the one loaded at startup), or None when none was."""
    return config.dotenv_file()


def elsewhere(cfg: AppConfig | None) -> tuple[Path, ...]:
    """Other ``.env`` files a key may have been put in by mistake, which are never read: the brain's work folder
    (where Claude, asked to add a key, tends to write one) and the current folder."""
    if cfg is None:
        return ()
    return (cfg.resolve_path(cfg.brain.claude.work_dir) / ".env", Path.cwd() / ".env")


def hint(*names: str, where: Path | None = None, then: str = "") -> str:
    """How to connect what needs ``names`` (all of one service), for the brain to pass on: "Gmail isn't connected
    yet. To connect it, the user ..., then adds two lines to the .env file ...: GMAIL_ADDRESS=<...> and ...; no
    restart is needed." ``where``: the exact file (else "the .env file in Octavia's folder"); ``then``: one more
    thing to do afterwards ("and asks me to connect Spotify")."""
    keys = [key(n) for n in names]
    first = keys[0]
    lines = [f"{k.name}={k.placeholder}" for k in keys]
    what = "a line" if len(lines) == 1 else f"{_count(len(lines))} lines"
    file = f"the .env file ({where})" if where is not None else "the .env file in Octavia's folder"
    added = f"adds {what} to {file}: {' and '.join(lines)}" if len(lines) > 1 else f"adds {lines[0]} to {file}"
    restart = "then restarts the assistant" if any(k.restart for k in keys) else "no restart is needed"
    extra = f", {then}" if then else ""
    return f"{first.service} isn't connected yet. To connect it, the user {first.get}, then {added}{extra}; {restart}."


def missing(*names: str, cfg: AppConfig | None = None, also: Iterable[Path] | None = None, then: str = "") -> str:
    """``hint`` for the keys that aren't set, naming the exact file, plus why the first of them looks missing when
    something in the files says so (a misspelled name, an empty line, the line in another ``.env``, a Windows
    environment variable in the way). ``also``: the other files to look in (default: ``elsewhere(cfg)``)."""
    unset = [n for n in names if not is_set(n)] or list(names)
    text = hint(*unset, where=env_file(), then=then)
    why = diagnose(unset[0], cfg, also)
    return f"{text} {why}" if why else text


def diagnose(
    name: str, cfg: AppConfig | None = None, also: Iterable[Path] | None = None, *, absent: bool = True
) -> str | None:
    """Why ``name`` reads as unset, from what the files say (key names only, never a value), or None when there's
    nothing to say. ``also``: other ``.env`` files it may have been put in by mistake (default: ``elsewhere(cfg)``).
    ``absent``: also say so when the file simply has no such line (a tool that needs the key does; a list of every
    key, most of them optional, doesn't)."""
    others_to_check = tuple(elsewhere(cfg) if also is None else also)
    file = env_file()
    if file is None:
        return None
    k = key(name)
    found = _keys(file)
    in_file = (found.get(name) or "").strip()
    if in_file and name in os.environ and not config.dotenv_supplied(name):
        problem = (
            f"that file has it, but a Windows environment variable named {name} is also set and takes priority over "
            "the file. Remove that environment variable and restart"
        )
    elif not file.is_file():
        problem = "that file doesn't exist"
    elif name in found and not in_file:
        problem = f"the line is there but has no {k.noun} after '{name}='"
        if k.clean is _text:
            problem += ". Put everything on that same line, separated by spaces"
    elif in_file:
        return None  # it's there: whatever's wrong isn't the file
    elif similar := similar_names(name, found):
        problem = f"that file has {', '.join(similar)} instead; the name must be exactly {name}"
    else:
        problem = "that file has no such line"
        if not absent and not any((_keys(p).get(name) or "").strip() for p in others_to_check):
            return None
    others = [
        path
        for path in dict.fromkeys(p.resolve() for p in others_to_check)
        if path != file.resolve() and (_keys(path).get(name) or "").strip()
    ]
    if others and not in_file:
        problem += f". It is in {' and '.join(map(str, others))}, which isn't read; move that line into {file}"
    return f"I looked for {name} in {file}: {problem}."


def override_note(name: str, cfg: AppConfig | None = None, same: Callable[[str, str], bool] | None = None) -> str:
    """A note for the user when the value in use comes from the process environment instead of ``.env`` (and says
    something else), or "" when the file's value is the one in use. ``same``: whether two values mean the same
    (the calendar compares the addresses in them, not the text).

    Such a variable often shows under neither User nor System variables in Windows' Environment Variables settings:
    it's inherited from whatever started the assistant (a launcher script, a terminal, a session opened before it was
    removed), so the note says so rather than sending the user to look for it there.
    """
    file = env_file()
    in_use = os.environ.get(name)
    if file is None or not (in_use or "").strip() or config.dotenv_supplied(name):
        return ""
    in_file = _keys(file).get(name)
    if in_file is not None and (
        same(in_use or "", in_file) if same else key(name).clean(in_use or "") == key(name).clean(in_file)
    ):
        return ""
    return (
        f"The {key(name).service} setting in use comes from a {name} variable in my own process environment, which "
        f"takes priority over {file}. It may not appear under User or System variables in Windows' settings: it's "
        "probably inherited from whatever started me (a launcher script, a terminal, or a session opened before it "
        "was changed). Remove it there, or start me from a fresh window, to use the file's value."
    )


def similar_names(name: str, names: Iterable[str]) -> list[str]:
    """Names in ``names`` that look like a slip for ``name`` (CALENDAR_ICS_URL for CALENDAR_ICS_URLS, GMAIL_PASSWORD
    for GMAIL_APP_PASSWORD): close in spelling, or sharing a distinctive word, and not another key of hers."""
    words = {w for w in name.split("_") if w not in _GENERIC and len(w) > 2}
    out = []
    for other in names:
        if other == name or other in _BY_NAME or other in INTERNAL:
            continue
        close = difflib.SequenceMatcher(None, name, other.upper()).ratio() >= 0.8
        if close or words & set(other.upper().split("_")):
            out.append(other)
    return out


def _keys(path: Path) -> dict[str, str | None]:
    try:
        return config.read_dotenv(path)
    except (OSError, ValueError):
        return {}


def _count(n: int) -> str:
    return {2: "two", 3: "three", 4: "four"}.get(n, str(n))


# ---- checking and tidying the file (octavia env, the doctor) ----


@dataclass(frozen=True)
class Finding:
    status: str  # "ok" | "unset" | "problem" | "unknown"
    name: str
    detail: str


def check(cfg: AppConfig) -> list[Finding]:
    """Every key: set, not set (and why, when the files say), and the lines in ``.env`` that no key of hers reads
    (with the key each was probably meant to be). Never a value."""
    file = env_file()
    out: list[Finding] = []
    for k in KEYS:
        if is_set(k.name):
            if _placeholder(config.current_env(k.name)):
                out.append(Finding("problem", k.name, "still holds a placeholder, not a real value"))
            else:
                out.append(Finding("ok", k.name, f"set ({k.service})"))
        elif k.commented:
            continue  # only for a setup that needs it: not set is the usual
        else:
            why = diagnose(k.name, cfg, absent=False)
            unset = "not set; she can't talk without it" if not k.optional else f"not set ({k.service})"
            out.append(Finding("problem" if why or not k.optional else "unset", k.name, why or unset))
    if file is not None:
        text = _raw_text(file)
        seen: dict[str, int] = {}
        for name in _names_in(text):
            seen[name] = seen.get(name, 0) + 1
        for name, times in seen.items():
            if times > 1:
                out.append(Finding("problem", name, f"is in {file} {times} times; only the last one counts"))
            if name not in _BY_NAME and name not in INTERNAL and name != cfg.brain.openai_compatible.api_key_env:
                meant = [n for n in _BY_NAME if name in similar_names(n, [name])]
                detail = "isn't read by Octavia" + (f"; did you mean {' or '.join(meant)}?" if meant else "")
                out.append(Finding("unknown", name, detail))
    return out


# What's left of a placeholder pasted over: "<token>", "[paste the address here]", "your_key_here".
_PLACEHOLDER = re.compile(r"^\s*[<\[].*[>\]]\s*$|\bpaste\b|\byour[\s_-]+\w+[\s_-]+here\b|^\s*x{3,}\s*$", re.IGNORECASE)


def _placeholder(raw: str | None) -> bool:
    return bool(raw) and _PLACEHOLDER.search(raw or "") is not None


_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", re.MULTILINE)


def _names_in(text: str) -> list[str]:
    return _LINE.findall(text)


def _raw_text(path: Path) -> str:
    """The file's text however Windows saved it (see ``config.read_dotenv``), or "" when it can't be read."""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def example() -> str:
    """The text of ``.env.example``: every key, grouped, with what it's for."""
    out = [
        "# Copy this file to .env (git ignores it) and fill in the keys you use. Only the Fish Audio key is required.",
        "# Values go straight after the =, on the same line; quotes are fine but not needed.",
        "# `uv run octavia env` checks this file, and `uv run octavia env tidy` sorts yours into these sections.",
        "# Claude needs nothing here: sign the assistant in once with `uv run octavia login`.",
    ]
    for section in dict.fromkeys(k.section for k in KEYS):
        out += ["", f"# ---- {section} ----"]
        for k in (k for k in KEYS if k.section == section):
            out += [f"# {line}" for line in _wrap(k.about)]
            out.append(f"{'# ' if k.commented else ''}{k.name}=")
    out += ["", "# OCTAVIA_HOME (which folder is hers) can't go in here: set it as a Windows environment variable."]
    return "\n".join(out) + "\n"


def _wrap(text: str, width: int = 116) -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    return lines + ([line] if line else [])


def tidy(text: str) -> str:
    """``.env`` text sorted into ``example()``'s sections, with its descriptions, every value kept exactly as written
    (the line itself is copied, never re-quoted). Lines no key reads go last, under "Not read by Octavia"; the
    user's own comments stay with the line below them, and the template's old comments are written afresh.

    Raises ValueError for a line that's neither ``NAME=value``, a comment nor blank (a value quoted across several
    lines, say): moving lines about could break it, so that file is left for the user to sort by hand.
    """
    template = _template_lines()
    blocks: dict[str, list[str]] = {}  # name -> its line(s), with the comments above them
    order: list[str] = []
    pending: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        match = _LINE.match(line)
        if match is None:
            if stripped and not stripped.startswith("#"):
                raise ValueError(f"line {number} isn't a NAME=value line or a comment")
            if stripped and stripped not in template:
                pending.append(line)
            continue
        name = match.group(1)
        if name not in blocks:
            order.append(name)
            blocks[name] = []
        blocks[name] += pending + [line]  # a repeated key keeps every line (the last still wins)
        pending = []
    out = example().splitlines()[:_HEADER_LINES]
    for section in dict.fromkeys(k.section for k in KEYS):
        names = [k.name for k in KEYS if k.section == section and k.name in blocks]
        if not names:
            continue
        out += ["", f"# ---- {section} ----"]
        for name in names:
            out += [f"# {line}" for line in _wrap(key(name).about)] + blocks[name]
    others = [n for n in order if n not in _BY_NAME]
    if others or pending:
        out += ["", _OTHERS]
        for name in others:
            out += blocks[name]
        out += pending
    return "\n".join(out) + "\n"


_HEADER_LINES = 4  # example()'s opening comment
_OTHERS = "# ---- Not read by Octavia ----"
# The comments of the .env.example before this list existed, which a copied .env still has: rewritten, not kept.
_OLD_TEMPLATE = (
    "# Copy this file to .env (which git ignores) and fill in your values.",
    "# Fish Audio (voice): create a key at https://fish.audio/app/api-keys",
    "# Optional: Fish Audio voice id (32 hex characters) from https://fish.audio/app/discovery",
    "# or your own cloned voice. Can also be set as tts.fish_audio.reference_id in config/local.yaml.",
    "# Claude: nothing needed here. The assistant has its own Claude login (in data/claude-home);",
    "# sign it in once with:  uv run octavia login",
    "# Do NOT set ANTHROPIC_API_KEY unless you set brain.claude.auth: api_key, because it would bill the API",
    "# instead of using your subscription.",
    "# Optional: TickTick (tasks, lists, habits). Make a token in TickTick on the web: your avatar, Settings,",
    "# Account & Security, API Token. Restart the assistant after adding it.",
)


def report(cfg: AppConfig) -> list[str]:
    """``octavia env``: which file is read, then every key and every stray line, one line each. Never a value."""
    file = env_file()
    lines = [f"Keys are read from {file}" + ("" if file is not None and file.is_file() else " (it doesn't exist yet)")]
    marks = {"ok": "ok  ", "unset": "--  ", "problem": "FIX ", "unknown": "??  "}
    for f in check(cfg):
        lines.append(f"  [{marks[f.status]}] {f.name}: {f.detail}")
    for name in (k.name for k in KEYS):
        if note := override_note(name, cfg):
            lines.append(f"  [note] {name}: {note}")
    return lines


def tidy_file(path: Path) -> tuple[bool, str]:
    """Rewrites ``path`` sorted into sections (``tidy``), keeping a copy of the old file next to it as ``.env.bak``
    (git ignores it). Returns (changed, what happened), for the user. Values are never shown."""
    if not path.is_file():
        return False, f"There's no {path} yet. Copy .env.example to it and fill in the keys you use."
    old = _raw_text(path)
    try:
        new = tidy(old)
    except ValueError as exc:
        return False, f"Left {path} as it is: {exc}. Tidy that line by hand, then run this again."
    if new.replace("\r\n", "\n") == old.replace("\r\n", "\n"):
        return False, f"{path} is already tidy."
    backup = path.with_name(path.name + ".bak")
    backup.write_bytes(path.read_bytes())
    path.write_text(new, encoding="utf-8", newline="\n")
    return True, f"Tidied {path} (every value kept as it was). The old file is in {backup}."


def masked(text: str) -> str:
    """``text`` (a .env file's) with every value replaced by whether it's set: to show a layout, never a secret."""
    out = []
    for line in text.splitlines():
        match = _LINE.match(line)
        if match is None:
            out.append(line)
            continue
        rest = line[match.end() :].strip().strip("\"'")
        out.append(f"{match.group(1)}={'(set)' if rest else ''}")
    return "\n".join(out) + "\n"


def _template_lines() -> frozenset[str]:
    """Comment lines ``example()`` (or the old template) writes: dropped from a file being tidied and written again."""
    ours = {line.strip() for line in example().splitlines() if line.strip().startswith("#")}
    return frozenset(ours | set(_OLD_TEMPLATE) | {_OTHERS})


def _quoted(value: str) -> str:
    """``value`` as it goes after ``NAME=``: bare when python-dotenv reads it back the same, else in single quotes
    (no ``${...}`` expansion there; only a backslash and a single quote need escaping)."""
    if not value or not re.search(r"[\s#'\"\\$]", value):
        return value
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def set_value(path: Path, name: str, value: str) -> None:
    """Put ``name=value`` in the ``.env`` at ``path`` (her settings window): the line that's there replaced (the
    last one, which is the one read), else the template's commented-out one, else a new line at the end. Every other
    line is kept as it was. An empty value keeps the line with nothing after the =. Raises ValueError for a value
    on more than one line, OSError when the file can't be written."""
    if "\n" in value or "\r" in value:
        raise ValueError("a key goes on one line")
    if name in _BY_NAME:
        value = key(name).clean(value) if value.strip() else ""
    line = f"{name}={_quoted(value)}"
    lines = _raw_text(path).splitlines() if path.exists() else []
    own = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=")
    commented = re.compile(rf"^\s*#\s*{re.escape(name)}\s*=")
    at = next((i for i in reversed(range(len(lines))) if own.match(lines[i])), None)
    if at is None:
        at = next((i for i in range(len(lines)) if commented.match(lines[i])), None)
    if at is None:
        if lines and lines[-1].strip():
            lines.append("")
        if name in _BY_NAME:
            lines += [f"# {text}" for text in _wrap(key(name).about)]
        lines.append(line)
    else:
        lines[at] = line
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    from octavia.core.live_settings import replace

    replace(tmp, path)
