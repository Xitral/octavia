"""A terminal: ``run_command`` runs one PowerShell command and gives her what it printed.

How risky a command is gets judged from its text (``classify``), and only ever raises the risk:

* **Read-only** (``SAFE``): every command in it is on a short list of ones that only look (``Get-ChildItem``,
  ``git status``, ``python --version``), with nothing that writes a file or calls a method. These just run.
* **Changes** (``CONFIRM``): anything else, such as ``npm install`` or ``mkdir``. She asks first, unless the
  user's own words plainly asked for it (``core/consent.py``); one yes covers the task's next ordinary changes.
* **High-risk** (``CONFIRM``, flagged by ``inspect_risk`` with a scope of its own): deleting or overwriting,
  stopping programs, installing, system settings, the internet, secrets (``.env``, ``$env:``), or any command
  after the turn has read outside content such as a web page. Always asked, one command at a time.
* **Blocked** (``RESTRICTED``, denied by ``permissions.restricted``): running downloaded or encoded code, disk
  and boot tools, security settings (Defender, execution policy, accounts, ACLs), and changing her own code,
  config or data, which only goes through self-improvement and its checks.

The text rules are a guardrail, not a sandbox: PowerShell can say the same thing many ways, so anything the
read-only list doesn't recognise needs a yes. The child also runs with every secret-looking environment variable
removed, with no window and no input, and is killed with its whole process tree when it runs out of time.

This file is protected from self-improvement builds (``evolve/policy.py``): a build can't loosen its own rules.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from octavia.config import find_project_root
from octavia.tools.base import RiskFinding, RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

MAX_OUTPUT = 8000  # characters of output she gets back; the start and the end are kept
MAX_TIMEOUT_S = 300

# --------------------------------------------------------------------------- reading a command


@dataclass(frozen=True)
class Verdict:
    """What a command would do: its risk, the kind of high-risk thing it is ("" for none) and, for the question,
    "Want me to run <command>, which <reason>?"."""

    risk: RiskLevel
    kind: str = ""
    reason: str = ""


def _words(*names: str) -> re.Pattern[str]:
    """Any of ``names`` as a command word: at the start of a segment, or after a pipe, ``;``, ``(``, ``{`` or ``&``.
    Case doesn't matter, and ``.exe`` may follow."""
    alts = "|".join(re.escape(n) for n in names)
    return re.compile(rf"(?:^|[|;&({{\n]|&&|\|\|)\s*(?:{alts})(?:\.exe)?(?=$|[\s;|)}}])", re.IGNORECASE)


# Blocked outright: what can't be taken back, hides what it runs, or weakens the computer's security.
_BLOCKED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:invoke-expression|iex)\b", re.I), "runs code it builds from text"),
    (re.compile(r"(?:^|\s)-e(?:nc(?:odedcommand)?|c)?\s+[A-Za-z0-9+/=]{16,}", re.I), "runs hidden, encoded code"),
    (re.compile(r"frombase64string|downloadstring|downloadfile|net\.webclient", re.I), "runs or fetches hidden code"),
    (re.compile(r"\|\s*(?:powershell|pwsh|cmd|bash|sh|python|py|node)(?:\.exe)?\b", re.I), "pipes text into a shell"),
    (
        _words(
            *"""format format-volume clear-disk initialize-disk diskpart bcdedit bootrec vssadmin wbadmin cipher
            takeown icacls cacls set-executionpolicy set-mppreference add-mppreference remove-mppreference net net1
            new-localuser set-localuser remove-localuser add-localgroupmember disable-computerrestore reagentc
            """.split()
        ),
        "changes disks, booting or the computer's security",
    ),
    (re.compile(r"-verb\s+runas\b|\brunas(?:\.exe)?\b|\bgsudo\b|\bsudo\b", re.I), "runs as administrator"),
    (
        re.compile(r"\b(?:remove-item|rm|del|erase|rd|rmdir|ri)\b[^|;]*\b[a-z]:\\?(?:\s|$|\*|[\"'])", re.I),
        "deletes a whole drive",
    ),
]
# Blocked unless the command only looks.
_SYSTEM_FOLDERS = re.compile(r"\\windows\\|\\program files|\\programdata\\microsoft", re.I)

# Always asked, each command on its own: (kind, reason, pattern).
_HIGH_RISK: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "delete",
        "deletes or overwrites files",
        re.compile(
            r"\b(?:remove-item|rm|del|erase|rd|rmdir|ri|clear-content|clear-recyclebin|set-content|out-file|"
            r"git\s+(?:clean|reset\s+--hard|checkout\s+--|restore|stash\s+(?:drop|clear)|branch\s+-d|"
            r"push\s+(?:-f|--force)))\b|(?<![>2])>(?![>&])|-force\b",
            re.I,
        ),
    ),
    (
        "close",
        "stops running programs",
        _words("stop-process", "kill", "spps", "taskkill", "stop-service", "restart-service", "suspend-service"),
    ),
    (
        "power",
        "restarts, shuts down or logs off the computer",
        _words("shutdown", "restart-computer", "stop-computer", "logoff", "rundll32"),
    ),
    (
        "install",
        "installs or uninstalls software",
        re.compile(
            r"\b(?:winget|choco|scoop|msiexec|install-module|install-package|uninstall-package|uninstall-module|"
            r"add-appxpackage|remove-appxpackage)\b|\b(?:pip|pip3|uv|npm|pnpm|yarn)\b[^|;]*\b(?:install|uninstall|"
            r"remove|add|i|un)\b|\b(?:npx|pipx|uvx)\b",
            re.I,
        ),
    ),
    (
        "system",
        "changes system settings",
        re.compile(
            r"\b(?:reg|regedit|setx|sc|netsh|schtasks|bcdedit|powercfg|wmic|set-itemproperty|new-itemproperty|"
            r"remove-itemproperty|set-service|new-service|register-scheduledtask|unregister-scheduledtask|"
            r"new-netfirewallrule|set-netfirewallprofile|set-netadapter|setenvironmentvariable)(?:\.exe)?\b|"
            r"\bhk(?:lm|cu|cr|u|cc)\b|\bhkey_",
            re.I,
        ),
    ),
    (
        "internet",
        "reaches the internet",
        re.compile(
            r"(?<![.\w])(?:invoke-webrequest|iwr|invoke-restmethod|irm|curl|wget|start-bitstransfer|ssh|scp|sftp|ftp|"
            r"test-netconnection|tnc|ping|nslookup|resolve-dnsname|tracert|send-mailmessage|"
            r"git\s+(?:push|pull|fetch|clone))\b|https?://",
            re.I,
        ),
    ),
    (
        "secrets",
        "can show passwords or keys",
        re.compile(
            r"\.env\b|\benv:|\$env\b|\bprintenv\b|^\s*set\s*$|\bid_(?:rsa|ed25519|ecdsa)\b|\.ssh\b|\.pem\b|\.pfx\b|"
            r"\.key\b|credential|password|secret|token|\bcookies\b|login data|\.kdbx\b|cmdkey|vaultcmd",
            re.I,
        ),
    ),
]

# Commands that only look. A segment is read-only when its first word is one of these (and, for the ones mapped
# to a pattern, the rest of it matches that pattern too).
_READ_ONLY_WORDS = {
    *"""get-childitem gci ls dir get-item gi get-content gc cat type get-location gl pwd set-location sl cd chdir
    push-location pushd pop-location popd test-path resolve-path rvpa split-path join-path get-itemproperty gp
    get-filehash get-acl get-process ps gps get-service gsv get-date get-computerinfo get-volume get-psdrive gdr
    get-disk get-partition get-netadapter get-netipaddress get-command gcm get-help help get-module get-host
    get-uptime get-hotfix get-ciminstance get-printer get-appxpackage get-startapps get-culture get-timezone
    get-winevent get-clipboard get-history h select-object select where-object where ?
    sort-object sort group-object group measure-object measure format-table ft format-list fl format-wide fw
    out-string select-string sls findstr compare-object diff convertto-json convertfrom-json convertto-csv
    convertfrom-csv convertto-html write-output echo write-host foreach-object foreach % tree whoami hostname
    systeminfo tasklist where.exe ver vol driverquery wc head tail more
    """.split(),
}
_READ_ONLY_ARGS: dict[str, re.Pattern[str]] = {
    "git": re.compile(
        r"^git\s+(?:status|log|diff|show|blame|ls-files|ls-tree|rev-parse|describe|shortlog|reflog\s*$|"
        r"branch(?:\s+(?:-a|-r|-v|-vv|--list|--show-current))*\s*$|remote(?:\s+-v)?\s*$|tag\s*$|config\s+--get)\b",
        re.I,
    ),
    "ipconfig": re.compile(r"^ipconfig(?:\.exe)?(?:\s+/all)?\s*$", re.I),
    "pip": re.compile(r"^pip3?\s+(?:list|show|freeze|--version)\b", re.I),
    "pip3": re.compile(r"^pip3?\s+(?:list|show|freeze|--version)\b", re.I),
    "npm": re.compile(r"^npm\s+(?:ls|list|view|outdated|--version|-v)\b", re.I),
    "uv": re.compile(r"^uv\s+(?:--version|pip\s+(?:list|show|freeze)|tree)\b", re.I),
    "winget": re.compile(r"^winget\s+(?:list|search|show|--version)\b", re.I),
}
for _program in "python py node java dotnet code pwsh powershell rustc cargo go gcc ffmpeg deno bun claude".split():
    _READ_ONLY_ARGS[_program] = re.compile(rf"^{_program}(?:\.exe)?\s+(?:--version|-v|-V|version)\s*$", re.I)

_SEGMENTS = re.compile(r"&&|\|\||[|;{}()\n]")
# Not commands: a variable or a property of one, a property of what's in brackets (".Count"), an operator or
# parameter ("-join"), a number.
_NOT_A_COMMAND = re.compile(r"^(?:\$[\w:]+(?:\.\w+)*|(?:\.\w+)+|-\w+|\d[\w.]*)$")
# Methods that only read or reshape a value ("$PSVersionTable.PSVersion.ToString()", "$name.Trim()"), whatever object
# they're called on. Not Replace: a file's .Replace() replaces the file.
_READ_METHODS = (
    "tostring|trim|trimstart|trimend|toupper|tolower|split|substring|contains|startswith|endswith|indexof|"
    "lastindexof|padleft|padright|equals|gettype"
)
# Writing (a redirect), calling any other method, the call operator, a static member, an escape or a here-string:
# nothing the read-only list can vouch for.
_NOT_READ_ONLY = re.compile(
    rf">|\.(?!(?:{_READ_METHODS})\s*\()\w+\s*\(|::|(?:^|[\s;|({{])&|`|@[\"']|\[[\w.]+\]", re.IGNORECASE
)
# A calculated column for Select-Object or Format-Table, the usual way to show sizes:
# ``@{n='GB';e={[math]::Round($_.Length/1GB,1)}}``. Only a name in plain quotes, and an expression of the item's
# properties, numbers, arithmetic and rounding: reading those can't change anything. It's taken out before the rest
# of the command is judged, which must only look as well.
# The name: letters, digits, spaces and a little punctuation, never a quote, ``;``, ``|`` or ``$``: a "name" that could
# close a string around it would hide the commands after it.
_LABEL = r"(?:n|name|l|label)\s*=\s*(?:'[\w .%#/()-]*'|\"[\w .%#/()-]*\")"
# A bracket only for grouping: never right after a word or a space (``$_.Delete()`` or ``$_.Delete ()`` calls a method).
_EXPRESSION = (
    r"(?:e|expression)\s*=\s*\{(?:\s|\$_(?:\.\w++)++|\d++(?:\.\d++)?+(?:kb|mb|gb|tb)?+|\[math\]::round\(|"
    r"(?<![\w\s])\(|[-+*/),])*+\}"
)
_CALCULATED = re.compile(
    rf"@\{{\s*(?:{_LABEL}\s*;\s*{_EXPRESSION}|{_EXPRESSION}\s*;\s*{_LABEL})\s*;?\s*\}}", re.IGNORECASE
)


def _quoted(command: str) -> list[bool]:
    """For each character of ``command``, whether it's inside a quoted string."""
    inside, quote = [], ""
    for char in command:
        inside.append(bool(quote))
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
    return inside


def _without_columns(command: str) -> str:
    """``command`` with its size columns (``_CALCULATED``) taken out, only where one really starts: never inside a
    string, where it would only be text."""
    inside = _quoted(command)
    return _CALCULATED.sub(lambda m: m.group(0) if inside[m.start()] else "column", command)


def _read_only(command: str) -> bool:
    """Whether every command in ``command`` only looks."""
    command = _without_columns(command)
    if _NOT_READ_ONLY.search(command):
        return False
    for raw in _SEGMENTS.split(command):
        segment = raw.strip()
        if not segment:
            continue
        first = segment.split()[0].lower()
        if _NOT_A_COMMAND.match(first):
            continue
        name = first.removesuffix(".exe")
        pattern = _READ_ONLY_ARGS.get(name)
        if pattern is not None:
            if not pattern.match(segment) or "--output" in segment.lower():
                return False
        elif first not in _READ_ONLY_WORDS and name not in _READ_ONLY_WORDS:
            return False
    return True


# Starting a download or job that carries on after the command returns: nothing would watch it, and her process tree
# isn't a safe place for it (a BITS transfer and a hidden curl both died quietly this way while she said one was
# running). Those go through start_download and run_long_command (``long_jobs``), which are tracked.
_DOWNLOADERS = frozenset(
    """curl wget wget2 aria2c bitsadmin invoke-webrequest iwr invoke-restmethod irm huggingface-cli hf yt-dlp
    youtube-dl git ollama rclone""".split()
)
# Programs that run whatever they're given: started on their own, they're untracked when hidden, given a script, or
# told to download.
_RUNNERS = frozenset("pwsh powershell cmd python python3 py pythonw node wscript cscript bash wsl".split())
_DOWNLOAD_WORDS = re.compile(
    r"(?<![\w-])(?:" + "|".join(re.escape(d) for d in sorted(_DOWNLOADERS)) + r")(?:\.exe)?(?![\w-])|download|"
    r"(?<![\w-])-(?:file|windowstyle\s+['\"]?hidden)\b|\bhidden\b",
    re.IGNORECASE,
)
_ALWAYS_UNTRACKED = re.compile(
    r"(?<![\w-])(?:start-bitstransfer|bitsadmin|start-job|start-threadjob|register-scheduledjob)(?![\w-])|&\s*$|"
    r"win32_process\b[^;|]*\bcreate\b|\bcreate\b[^;|]*\bwin32_process\b",
    re.IGNORECASE,
)
_START_PROCESS = re.compile(r"(?<![\w-])(?:start-process|saps|start)(?![\w-])([^;|]*)", re.IGNORECASE)
# Start-Process parameters that take a value, which isn't the program.
_VALUED = frozenset(
    "-windowstyle -workingdirectory -verb -redirectstandardinput -redirectstandardoutput -redirectstandarderror "
    "-credential -argumentlist -args".split()
)
UNTRACKED_REASON = (
    "That would start a download or job that keeps running after the command returns, where nothing keeps track of "
    "it, so I didn't run it. Use start_download for a file from the web, or run_long_command for a long command: "
    "those run in a process of their own, survive restarts, and the user hears when they finish or fail."
)


def _started_program(rest: str) -> tuple[str, str]:
    """For what follows ``Start-Process``: (the program's name, lower-case and without folder or ``.exe``; the rest)."""
    tokens = rest.replace(",", " ").split()
    i = 0
    while i < len(tokens):
        token = tokens[i].strip("'\"").lower()
        if token == "-filepath" and i + 1 < len(tokens):
            i += 1
            break
        if token in _VALUED:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        break
    if i >= len(tokens):
        return "", rest
    program = re.split(r"[\\/]", tokens[i].strip("'\""))[-1].lower().removesuffix(".exe")
    return program, " ".join(tokens[:i] + tokens[i + 1 :])


def starts_untracked(command: str) -> bool:
    """Whether ``command`` starts something that outlives it: a background transfer or job, a process made through
    WMI, or ``Start-Process`` of a downloader, or of a shell or interpreter that's hidden, given a script or told to
    download. ``Start-Process … -Wait`` waits, so it isn't; nor is opening an app or a web page."""
    if _ALWAYS_UNTRACKED.search(command):
        return True
    for match in _START_PROCESS.finditer(command):
        program, rest = _started_program(match.group(1))
        if not program or re.search(r"(?<![\w-])-wait\b", rest, re.IGNORECASE):
            continue
        if program in _DOWNLOADERS:
            return True
        if program in _RUNNERS and _DOWNLOAD_WORDS.search(rest):
            return True
    return False


def _own_paths() -> list[str]:
    """Her own code, config and data, as lower-case text to look for in a command."""
    root = find_project_root().resolve()
    return [str(root).lower(), str(root).lower().replace("\\", "/")]


def _touches_own_files(command: str, cwd: Path) -> bool:
    text = command.lower()
    if any(p in text for p in _own_paths()):
        return True
    root = find_project_root().resolve()
    try:
        inside = cwd.resolve().is_relative_to(root)
    except OSError:
        inside = False
    return inside or bool(re.search(r"projects[\\/]+octavia\b|octavia[\\/]+(?:src|config|data)", text))


def classify(command: str, cwd: Path) -> Verdict:
    """What running ``command`` in ``cwd`` would do (see the module docstring)."""
    for pattern, reason in _BLOCKED:
        if pattern.search(command):
            return Verdict(RiskLevel.RESTRICTED, "blocked", reason)
    if _read_only(command):
        for kind, reason, pattern in _HIGH_RISK:
            if kind in ("secrets", "internet") and pattern.search(command):
                return Verdict(RiskLevel.CONFIRM, kind, reason)
        return Verdict(RiskLevel.SAFE)
    if _touches_own_files(command, cwd):
        return Verdict(RiskLevel.RESTRICTED, "blocked", "changes my own code, settings or data")
    if _SYSTEM_FOLDERS.search(command):
        return Verdict(RiskLevel.RESTRICTED, "blocked", "changes Windows or installed programs")
    for kind, reason, pattern in _HIGH_RISK:
        if pattern.search(command):
            return Verdict(RiskLevel.CONFIRM, kind, reason)
    return Verdict(RiskLevel.CONFIRM)


# --------------------------------------------------------------------------- saying what a command does

# What a command does, by its first word, in words that finish "run a command to ...". {place} is the file or folder
# it names, said plainly ("the models folder", "local.yaml", "drive E"). These only say it looks: they describe a part
# of a command that only looks, never one that does more (``gci | ForEach-Object { $_.Delete() }``).
_LOOKS: list[tuple[frozenset[str], str]] = [
    (frozenset("get-childitem gci ls dir tree".split()), "list what's in {place}"),
    (frozenset("get-content gc cat type".split()), "read {place}"),
    (frozenset("select-string sls findstr".split()), "search {place} for some text"),
    (frozenset("test-path".split()), "check whether {place} exists"),
    (frozenset("get-item gi get-itemproperty gp get-filehash".split()), "look at {place}"),
    (frozenset("get-process ps gps tasklist".split()), "check which programs are running"),
    (frozenset("get-bitstransfer".split()), "check on downloads"),
    (
        frozenset("get-ciminstance get-computerinfo systeminfo get-volume get-psdrive get-disk".split()),
        "check this PC's hardware",
    ),
    (frozenset("get-service gsv".split()), "check Windows services"),
]
_PLAIN: list[tuple[frozenset[str], str]] = [
    *_LOOKS,
    (frozenset("start-bitstransfer".split()), "start a download"),
    (frozenset("invoke-webrequest iwr invoke-restmethod irm curl wget".split()), "get something from the internet"),
    (frozenset("remove-item rm del erase rd rmdir ri".split()), "delete {place}"),
    (frozenset("copy-item copy cp cpi".split()), "copy {place}"),
    (frozenset("move-item move mv mi rename-item ren rni".split()), "move or rename {place}"),
    (frozenset("new-item ni mkdir md".split()), "create {place}"),
    (frozenset("set-content out-file add-content clear-content".split()), "write to {place}"),
    (frozenset("stop-process kill spps taskkill".split()), "stop a program"),
    (frozenset("start-process saps start".split()), "start a program"),
    (frozenset("winget choco scoop msiexec".split()), "install or check software"),
]
_PROGRAMS = {
    "git": "Git",
    "python": "Python",
    "py": "Python",
    "node": "Node",
    "npm": "npm",
    "pip": "pip",
    "uv": "uv",
    "nvidia-smi": "the graphics card tool",
}
_STATEMENTS = re.compile(r";|&&|\|\||\n")
_PIECES = re.compile(r";|&&|\|\|?|\n")  # statements, and the commands of a pipeline


_GOING_TO = re.compile(r"(?:cd|chdir|sl|set-location|pushd|push-location|popd|pop-location)(?:\s|$)", re.I)


def _statements(command: str, at: re.Pattern[str] = _STATEMENTS) -> list[str]:
    """``command`` split into its statements (at ``;``, ``&&``, ``||`` and new lines; ``_PIECES`` at pipes too), but
    not inside quotes, brackets or braces: ``@{n='GB';e={...}}`` is one column of one statement."""
    parts: list[str] = []
    depth, quote, start = 0, "", 0
    for i, char in enumerate(command):
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char in "({[":
            depth += 1
        elif char in ")}]":
            depth = max(0, depth - 1)
        elif depth == 0 and i >= start and (found := at.match(command, i)):
            parts.append(command[start:i])
            start = i + len(found.group(0))
    parts.append(command[start:])
    return parts


# A path in quotes (it may have spaces), a path from a drive, ".\", "..\" or "~\", or a file name with an extension.
_A_PATH = re.compile(
    r"""['"]((?:[a-z]:|\.{1,2}|~)?[\\/][^'"]*)['"]"""
    r"""|((?:[a-z]:[\\/]|\.{1,2}[\\/]|~[\\/])[^'"|;,{}()\s]*)"""
    r"""|(?<![\w$.\-*])(\w[\w.-]*\.[a-z][a-z0-9]{0,4})(?![\w(])""",
    re.I,
)


def _place(statement: str) -> str:
    """The first file or folder ``statement`` names, said plainly; "a folder" when it names none."""
    for found in _A_PATH.finditer(statement):
        text = next(g for g in found.groups() if g is not None).rstrip("\\/")
        if re.fullmatch(r"[a-z]:", text, re.I):
            return f"drive {text[0].upper()}"
        name = re.split(r"[\\/]", text)[-1]
        if not name or name in (".", ".."):
            continue
        if "*" in name:
            return "some files"
        return name if "." in name.strip(".") else f"the {name} folder"
    return "a folder"


def _say_piece(piece: str, changes: bool) -> str:
    """One command of a pipeline in words, or "" when its first word says nothing. ``changes``: the piece doesn't only
    look, so a looking phrase ("list what's in ...") would hide what it does."""
    words = re.sub(r"^\$\w+\s*=\s*|^[(\s]+", "", piece).split()
    if not words:
        return ""
    first = words[0].lower().removesuffix(".exe")
    for names, phrase in _LOOKS if not changes else []:
        if first in names:
            return phrase.format(place=_place(piece))
    for names, phrase in _PLAIN[len(_LOOKS) :]:
        if first in names:
            return phrase.format(place=_place(piece))
    if first in _PROGRAMS:
        return f"use {_PROGRAMS[first]}"
    if changes and (first in _READ_ONLY_WORDS or any(first in names for names, _ in _LOOKS)):
        return "change something"  # a looking command made to do more (a method call, a loop that deletes)
    return f"use {first}" if re.fullmatch(r"[a-z][\w-]{0,19}", first) else ""


def plain_words(command: str) -> str:
    """What ``command`` does, short enough to say ("list what's in the models folder"): the first part of it that does
    more than look (or, when it only looks, its first part) in words, and how many more statements there are. Never
    the command itself, which nobody can follow by ear."""
    # Moving to a folder first isn't what the command is for.
    statements = [s.strip() for s in _statements(command) if s.strip() and not _GOING_TO.match(s.strip())]
    if not statements:
        return "change folders" if command.strip() else "do nothing"
    pieces = [p.strip() for s in statements for p in _statements(s, _PIECES) if p.strip()]
    changing = next((p for p in pieces if not _read_only(p)), None)
    said = _say_piece(changing, changes=True) if changing is not None else ""
    if not said and changing is not None:
        said = "change something"
    for piece in pieces if not said else []:
        if said := _say_piece(piece, changes=False):
            break
    said = said or "do something PowerShell can do"
    more = len(statements) - 1
    if more:
        said += f", plus {more} more step" + ("s" if more > 1 else "")
    return said


# --------------------------------------------------------------------------- running a command

_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION|OCTAVIA|ANTHROPIC|CLAUDE", re.I)
_PREAMBLE = (
    "$ProgressPreference = 'SilentlyContinue'; "
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $OutputEncoding = [System.Text.Encoding]::UTF8; "
)


def _shell() -> str:
    """PowerShell 7 when it's installed, else Windows PowerShell (64-bit under a 64-bit Python)."""
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"


def _child_env() -> dict[str, str]:
    """Her environment without anything that looks like a key, token or password."""
    return {k: v for k, v in os.environ.items() if not _SECRET_NAME.search(k)}


def _clip(text: str) -> str:
    text = text.replace("\r\n", "\n").strip()
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    return f"{text[:half]}\n… ({len(text) - MAX_OUTPUT} characters cut) …\n{text[-half:]}"


def _kill_tree(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/T", "/F", "/PID", str(pid)],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )


def _run(command: str, cwd: Path, timeout_s: float) -> tuple[int | None, str]:
    """Run ``command`` and wait for it: (exit code, or None when it ran out of time; everything it printed)."""
    script = base64.b64encode((_PREAMBLE + command).encode("utf-16-le")).decode("ascii")
    proc = subprocess.Popen(
        [_shell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", script],
        cwd=str(cwd),
        env=_child_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        out, _ = proc.communicate()
        return None, out.decode("utf-8", errors="replace")
    return proc.returncode, out.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- the tool


class RunCommandArgs(ToolArgs):
    command: str = Field(min_length=1, max_length=4000, description="One PowerShell command line")
    cwd: str = Field("", description="Folder to run it in (default: the user's home folder)")
    timeout_s: int = Field(60, ge=1, le=MAX_TIMEOUT_S, description="Stop it after this many seconds")


def _folder(args: RunCommandArgs) -> Path:
    return Path(os.path.expandvars(args.cwd)).expanduser() if args.cwd else Path.home()


def _untrusted_turn(ctx: ToolContext) -> bool:
    """Whether the running turn has read outside content (a web page could be steering her) or was texted from
    elsewhere: then no change goes ahead on an earlier yes or on the user's words alone."""
    get = ctx.extras.get("turn")
    if not callable(get):
        return False
    try:
        turn = get()
    except Exception:
        return True  # can't tell: treat it as having read something
    return bool(turn is not None and (turn.outside_content or turn.remote))


class RunCommand(Tool):
    name = "run_command"
    description = (
        "Run one PowerShell command on the user's Windows PC and get its output: list or search files, check "
        "versions, git status, build or test a project, run a script. Read-only commands just run; ones that "
        "change things ask the user first; running downloaded or encoded code, disk, boot and security tools, "
        "and changing your own code or settings are blocked. Not for programs that keep running (dev servers, "
        "apps): open those another way. Not for downloads or anything over a few minutes: start_download and "
        "run_long_command track those. No input can be typed into it."
    )
    Args = RunCommandArgs
    risk = RiskLevel.SAFE  # raised per command by ``assess_risk`` and ``inspect_risk``
    category = "system"
    timeout_s = MAX_TIMEOUT_S + 15.0

    def assess_risk(self, args: RunCommandArgs) -> RiskLevel:
        return classify(args.command, _folder(args)).risk

    async def inspect_risk(self, args: RunCommandArgs, ctx: ToolContext) -> RiskFinding | None:
        verdict = classify(args.command, _folder(args))
        # The scope names the exact command, so a yes to one never covers a different one.
        if verdict.kind and verdict.risk is RiskLevel.CONFIRM:
            return RiskFinding(RiskLevel.CONFIRM, f"{verdict.kind}:{args.command}", verdict.reason)
        if verdict.risk is RiskLevel.CONFIRM and _untrusted_turn(ctx):
            reason = "comes after reading something from outside, which could be steering me"
            return RiskFinding(RiskLevel.CONFIRM, f"outside:{args.command}", reason)
        return None

    async def describe_call(self, args: RunCommandArgs, ctx: ToolContext) -> str:
        # Said out loud: what it does in a few plain words, never the command itself (``call_detail`` shows that).
        return f"run a command to {plain_words(args.command)}"

    def call_detail(self, args: RunCommandArgs) -> str:
        command = args.command if len(args.command) <= 300 else args.command[:297] + "…"
        return f"{command}  (in {args.cwd})" if args.cwd else command

    async def precheck(self, args: RunCommandArgs, ctx: ToolContext) -> str | None:
        if starts_untracked(args.command):
            return UNTRACKED_REASON
        if not _folder(args).is_dir():
            return f"There's no folder {_folder(args)}."
        return None

    async def run(self, args: RunCommandArgs, ctx: ToolContext) -> ToolResult:
        folder = _folder(args)
        if starts_untracked(args.command):
            return ToolResult.error(UNTRACKED_REASON)
        if not folder.is_dir():
            return ToolResult.error(f"There's no folder {folder}.")
        try:
            code, output = await asyncio.to_thread(_run, args.command, folder, float(args.timeout_s))
        except OSError as exc:
            return ToolResult.error(f"Couldn't start PowerShell: {exc}")
        shown = _clip(output) or "(no output)"
        data = {"exit_code": code, "cwd": str(folder)}
        if code is None:
            return ToolResult.error(f"Stopped it after {args.timeout_s}s. Output so far:\n{shown}", data)
        if code != 0:
            return ToolResult.error(f"Exit code {code}:\n{shown}", data)
        return ToolResult.success(shown, data)


TOOLS = [RunCommand]
