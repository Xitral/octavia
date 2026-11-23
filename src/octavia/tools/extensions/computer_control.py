"""Mouse and keyboard control: she clicks, types, presses keys and scrolls on the user's PC, so she can finish a
task herself (open Spotify, click Liked Songs, play the newest song) instead of telling the user where to click.

What the user chose: she clicks and types freely, but asks before anything that sends, buys, deletes, installs or
types into a password box, and moving the mouse or pressing the stop hotkey (Ctrl+Alt+K) stops her at once.

* Only while the user is there: every tool refuses outside a running user turn (``ctx.extras["turn"]``).
* What a call would really do is judged from the screen, not from what the model says (``inspect_risk``): the
  element under the click, or with the keyboard focus, read with UI Automation, with its window and process
  (``_uia.Target``). The words that make an action risky are in one table, ``RISKS``; the rules are
  ``classify_click``, ``classify_typing`` and ``classify_keys``. A risky call is raised to CONFIRM, so the executor
  asks the user first. The model's own ``label`` and ``risky`` (click) count too, and can only add risk.
* Every input into a console, the Run box or the registry editor asks, command by command: the original brief rules
  out unrestricted shell access, and typing into a shell is exactly that.
* Right before a click the spot is looked at again, and she doesn't click if it isn't what the risk check saw (the
  user may have taken a while to answer); typing and keys check the keyboard focus the same way.
* The user taking over stops her for the rest of the turn (``_input.Watch``): moving the mouse, typing or clicking,
  or the stop hotkey, which interrupts the turn.
* Scrolling also stops for the rest of the turn once the user is heard talking (``ctx.extras["heard_talking"]``),
  and waits for a look at the screen between two scrolls, so reading a long chat never skips lines unseen.
* When the avatar can point, she points at the target first and clicks once she's beside it: the user sees what
  she's about to click, and the click never lands on her.
* What UI Automation can't see into (``drawn_surface``: a remote screen streamed into a web page, a canvas) is
  checked on the screen itself (``_landing``): a click there that changed nothing is tried once more slowly, then
  said honestly as not having landed; typing into one, after a click that landed in it, goes in as key presses by
  scan code (streamed apps pass on keys, not text), then as a paste, and is said as failed when neither shows.

Windows keeps some things out of reach of any normal app's input: apps running as administrator, UAC prompts, the
lock screen and Ctrl+Alt+Delete.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import logging
import re
import sys
import weakref
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Any, Literal, Protocol, Self

from pydantic import ConfigDict, Field, PrivateAttr, field_validator, model_validator

from octavia.core import graded_work
from octavia.core.claims import SAID_SENT, SENT_STUCK, SENT_UNKNOWN, SENT_WENT
from octavia.core.events import PermissionResolved
from octavia.core.submit_sites import free_step, is_step_button, on_submit_site
from octavia.platform.windows import MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, MOD_SHIFT, MOD_WIN, parse_hotkey
from octavia.tools.base import RiskFinding, RiskLevel, Tool, ToolArgs, ToolContext, ToolResult, TurnInfo
from octavia.tools.executor import ALREADY_APPROVED, ASKED_FOR, ON_ITS_OWN
from octavia.tools.extensions import _input, _landing, _uia, clipboard
from octavia.tools.extensions._input import (
    KEYS,
    SCAN_LSHIFT,
    SCAN_RETURN,
    VK_LSHIFT,
    VK_RETURN,
    KeyCombo,
    Watch,
    chunks,
    clean_text,
    combo_strokes,
    parse_keys,
    paste_strokes,
    scan_strokes,
    text_strokes,
)
from octavia.tools.extensions._uia import Target
from octavia.tools.extensions.screen_capture import SCREEN, Rect, ScreenMemory, pointing_avatar
from octavia.tools.extensions.screen_pointing import avatar_pids
from octavia.tools.extensions.screen_wait import BEFORE, InputSnapshots

log = logging.getLogger(__name__)

NOT_IN_TURN = "I only use the mouse and keyboard while you're talking with me."
TAKEN_OVER = "The user took over the mouse and keyboard, so I stopped. Don't use them again unless they ask."
INTERRUPTED = "The user interrupted, so I stopped using the mouse and keyboard."
# The last line of every mouse and keyboard tool's description.
TOOK_OVER_RULE = "If you're told the user took over, stop: no mouse or keyboard again this turn unless they ask."
NOT_WINDOWS = "Mouse and keyboard control only works on Windows."
USER_TALKING = (
    "The user started talking, so I stopped scrolling. Don't scroll again this turn: tell them briefly what you've "
    "read so far and let them speak."
)
LOOK_FIRST = (
    "I've scrolled since my last look at the screen, so I didn't scroll again: what went past would be skipped "
    "unseen. Look first (look_at_screen, or find_on_screen when you're after one thing)."
)
NO_SHOT = "I don't have a recent screenshot. Take one with look_at_screen, or find the control with find_on_screen."
SCREEN_BUSY = (
    "A screen task is using the mouse and keyboard right now. Hand this to start_screen_task to do after it, or stop "
    "it first with stop_screen_task if the user wants this instead."
)

LABEL_CHARS = 60
POINT_WAIT_S = 0.7  # for the avatar to fly beside the target before the click
HOVER_S = 0.12  # after moving the pointer: hover effects show, and her window lets clicks through again
SETTLE_S = 0.05  # after her input, for Windows to take it in before the watch's new baseline
AFTER_S = 0.3  # after a click, for the app to answer it before the look at what it did
AFTER_BUDGET_S = 2.0  # for that look and the wait before it: the click went in, so it mustn't run into the timeout
BEFORE_LOOK_S = 1.0  # for the look at the keyboard focus before a click, to tell what it did
CANT_CHECK = "I couldn't check what it did (the app didn't answer in time), so look before going on."
CHUNK_CHARS = 16  # typed at a time, with the checks in between
KEY_GAP_S = 0.06  # between repeated key presses
FOCUS_BACK_S = 0.15  # for a window given the keys back from hers to put them where they were in it
# A slower click, for a drawn surface that showed nothing after a quick one: the pointer moved onto the spot (the far
# end of a stream sees it arrive), a rest there, and the button held down longer.
SLOW_STEP_S = 0.05
SLOW_HOVER_S = 0.3
SLOW_HOLD_S = 0.12
PASTE_HOLD_S = 0.5  # the pasted text stays on the clipboard this long: a streamed app reads it as the paste arrives
# After Enter in a message box, the waits before each read of what's left in it: a send can take a moment to clear it.
SENT_WAITS_S = (0.15, 0.3, 0.45)
SENT_TAIL_CHARS = 40  # the end of what was typed, looked for in the box afterwards
_INVISIBLE = str.maketrans("", "", "\ufeff\u200b\u200c\u200d\u2060")  # what an empty editor holds ("\ufeff\n")
SCROLL_STEP = 5  # wheel notches at a time, with the checks in between
READ_NOTCHES = 5  # a screenful less a few lines in a chat or web page: reading through, each view overlaps the last
PAGE_NOTCHES = 10  # more than this at once is more than a screenful in most apps
MAX_SHOT_AGE_S = 90.0  # an older screenshot or zoom no longer says where things are

# --------------------------------------------------------------------------- what makes an action risky


@dataclass(frozen=True)
class Risk:
    """A kind of risky action: its scope (``RiskFinding.scope``), the reason the user hears ("Want me to <do it>,
    which <reason>?"), and what names it, matched as whole words in any case."""

    scope: str
    reason: str
    words: tuple[str, ...] = ()  # in the name of a button, link or menu item (and in the dialog around an "OK")
    names: tuple[str, ...] = ()  # only as the whole name: "Exit", not "Exit full screen"
    hints: tuple[str, ...] = ()  # only in the dialog around a generic button ("OK", "Next")


# The words that make an action risky, most serious first. A word straight after a negation doesn't count: "Don't
# send" and "Cancel delete" are the safe choice. Around a generic button the words also count with an ending
# ("deleted", "installing"), since the dialog's text explains what the button does.
RISKS: tuple[Risk, ...] = (
    Risk("money", "moves money",
         words=("transfer", "send money", "withdraw", "sell", "trade", "cash out")),
    Risk("buy", "can spend money",
         words=("buy", "purchase", "pay", "pay now", "checkout", "check out", "place order", "place your order",
                "order now", "submit order", "confirm order", "complete order", "subscribe", "upgrade", "donate",
                "add funds", "confirm purchase", "complete purchase", "free trial", "rent", "pre-order", "preorder"),
         hints=("payment", "charged", "billing")),
    Risk("delete", "deletes or discards something",
         words=("delete", "remove", "erase", "trash", "discard", "empty", "clear all", "clear history", "clear data",
                "clear browsing data", "uninstall", "reset", "wipe", "overwrite", "don't save", "without saving",
                "format disk", "format drive", "replace the file", "replace the files"),
         hints=("permanently", "recycle bin", "quick format", "format usb drive", "format local disk",
                "formatting will erase", "erase all data", "overwritten")),
    Risk("install", "installs, runs or allows something",
         words=("install", "reinstall", "run", "run anyway", "allow", "grant", "accept", "agree", "trust",
                "enable content", "enable macros", "add extension", "add to chrome", "add to firefox"),
         hints=("installer", "installation", "setup", "setup wizard", "unknown publisher", "security warning",
                "user account control", "make changes to your device", "license agreement",
                "publisher could not be verified", "windows protected your pc")),
    Risk("send", "sends something",
         words=("send", "submit", "post", "repost", "publish", "tweet", "retweet", "share", "reply all")),
    Risk("close", "closes an app",
         names=("exit", "quit", "close window", "close all windows", "end task", "end process", "end process tree",
                "force quit", "force close")),
    Risk("power", "shuts down or restarts the computer",
         names=("shut down", "restart", "restart now", "update and restart", "update and shut down")),
    Risk("irreversible", "can't be undone",
         hints=("cannot be undone", "can't be undone", "can not be undone", "irreversible")),
)  # fmt: skip
_RISK = {risk.scope: risk for risk in RISKS}

# Buttons that do whatever the dialog around them says: an "OK" under "Delete these 3 files forever?" deletes.
GENERIC = frozenset(
    {"ok", "okay", "yes", "continue", "confirm", "apply", "done", "accept", "allow", "next", "start", "finish",
     "proceed", "go", "i understand"}
)  # fmt: skip
ACTION_KINDS = frozenset({"button", "split button", "menu item", "link", "check box", "radio button"})
_ITEM_KINDS = frozenset({"list item", "tree item", "grid item", "item"})  # a file in File Explorer, a row
_NEGATED = re.compile(r"\b(?:don't|dont|do not|never|not|no|cancel|stop|undo|skip|block|deny|decline|reject)\s+$")
_SAVE_PROMPT = re.compile(r"\b(?:save|saving|unsaved)\b")  # "No" under "Save changes to Untitled?" throws them away
_EDGES = " .,!?:;…*>›»<‹«()[]\"'"

# Where any input can run commands: consoles and shells, the Run box, the registry editor, management consoles.
SHELLS = frozenset(
    {"cmd.exe", "powershell.exe", "pwsh.exe", "powershell_ise.exe", "windowsterminal.exe", "wt.exe",
     "openconsole.exe", "conhost.exe", "mintty.exe", "bash.exe", "wsl.exe", "wslhost.exe", "ubuntu.exe",
     "alacritty.exe", "wezterm-gui.exe", "hyper.exe", "tabby.exe", "conemu.exe", "conemu64.exe", "putty.exe",
     "kitty.exe", "mobaxterm.exe", "termius.exe", "regedit.exe", "mmc.exe"}
)  # fmt: skip
SHELL_CLASSES = frozenset(
    {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "PseudoConsoleWindow", "mintty", "VirtualConsoleClass",
     "PuTTY"}
)  # fmt: skip
RUN_BOXES = frozenset({"run", "create new task"})  # the Run dialog (Win+R) and Task Manager's "Run new task"
# A terminal inside another app (VS Code's, a browser's developer console), but not a file called terminal.py.
_SHELL_WORDS = re.compile(r"\b(?:terminal|console|command prompt|powershell)\b(?!\.\w)")
_PYTHON = frozenset({"python.exe", "pythonw.exe"})  # with "Shell" in the title: IDLE's, which runs what's typed
# A notebook (Jupyter, in a browser or VS Code): Shift+Enter or Ctrl+Enter runs the cell's code.
_NOTEBOOK = re.compile(r"\bjupyter(?:lab)?\b|\.ipynb\b|\bcolab\b")
# A PDF open in the browser's own viewer: the page is named for the file ("Form 48.pdf"), and so is the window, with
# only the browser's name after it ("Form 48.pdf - Vivaldi"). A site showing a PDF names itself too ("Form 48.pdf -
# Google Drive - Vivaldi").
_PDF_FILE = re.compile(r"\.pdf$", re.IGNORECASE)
_PDF_WINDOW = re.compile(r"\.pdf - [^-]+$", re.IGNORECASE)
RUN_CELL_KEYS = frozenset({"shift+enter", "ctrl+enter", "alt+enter"})
_TITLED = frozenset({"document", "window", "title bar"})  # their names are what's shown, not what they are
# Where Enter runs what was typed: the Start menu's search, and File Explorer's address bar.
LAUNCHERS = frozenset({"searchhost.exe", "searchapp.exe", "searchui.exe", "startmenuexperiencehost.exe"})
# Windows' own shell, whose Restart and Shut down are the computer's.
_WINDOWS_SHELL = LAUNCHERS | {"explorer.exe", "shellexperiencehost.exe", "systemsettings.exe"}
# Where Enter sends a message.
MESSAGING = frozenset(
    {"discord.exe", "discordptb.exe", "discordcanary.exe", "slack.exe", "teams.exe", "ms-teams.exe", "msteams.exe",
     "whatsapp.exe", "whatsapp.root.exe", "telegram.exe", "signal.exe", "messenger.exe", "outlook.exe", "olk.exe",
     "thunderbird.exe", "hxoutlook.exe", "skype.exe", "element.exe", "mailspring.exe"}
)  # fmt: skip
EMAIL = frozenset({"outlook.exe", "olk.exe", "thunderbird.exe", "hxoutlook.exe", "mailspring.exe"})
BROWSERS = frozenset(
    {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe", "arc.exe", "chromium.exe",
     "waterfox.exe", "librewolf.exe", "floorp.exe", "zen.exe", "iexplore.exe"}
)  # fmt: skip
_MESSAGING_SITES = re.compile(
    r"\b(?:gmail|outlook|messenger|discord|slack|teams|whatsapp|telegram|signal|proton mail|yahoo mail|instagram)\b"
)
_EMAIL_SITES = re.compile(r"\b(?:gmail|outlook|proton mail|yahoo mail)\b")
# AI chats, where Enter sends a message too (only for telling whether it went: ``checks_send``).
CHAT_APPS = frozenset({"claude.exe", "chatgpt.exe"})
_CHAT_SITES = re.compile(r"\b(?:claude|chatgpt|gemini|copilot|perplexity)\b")
_SEARCH_BOX = re.compile(r"\b(?:search|find|filter|address|url)\b")
_INSTALLER = re.compile(r"setup|install|unins\d*\.|msiexec|bootstrapper")  # FooSetup.exe, unins000.exe
# Files that run code when opened: double-clicking one (or Enter on it) runs a program, so it asks like "Run" does.
# A single click opens only what's plainly a program (a browser's downloaded setup.exe): link texts like "Node.js"
# or "setup.py" on a web page are more often names than files.
_PROGRAM_FILE = r"exe|com|msi|msix|msixbundle|appx|appxbundle|appinstaller|bat|cmd|ps1|vbs|vbe|wsf|scr|hta|jar|reg|cpl"
_PROGRAM = re.compile(rf"\.(?:{_PROGRAM_FILE})\b")
# Shortcuts too: a .lnk runs whatever command line it holds, a .url or .scf can start programs, a .msc opens an
# admin console.
_RUNNABLE_FILE = re.compile(rf"\.(?:{_PROGRAM_FILE}|js|jse|wsh|pif|psm1|py|pyw|lnk|url|scf|msc)\b")
# The same, as File Explorer names the kind of file when it hides extensions ("Application", "Windows Batch File").
_RUNNABLE_TYPE = re.compile(
    r"\b(?:application|batch file|command script|script file|javascript file|powershell script|installer package"
    r"|registry entries|ms-dos|screen saver|html application|python file|windows script|shortcut"
    r"|internet shortcut|microsoft common console document)\b"
)

# Keys that are risky wherever they're pressed.
# The Run box, the menu with Terminal (Admin) in it, and Windows Terminal's drop-down ("quake") window.
COMMAND_KEYS = frozenset({"win+r", "win+x", "win+backquote"})
CLOSE_KEYS = frozenset({"alt+f4", "ctrl+q", "ctrl+shift+w", "ctrl+shift+q"})
DELETE_KEYS = frozenset({"delete", "shift+delete", "ctrl+shift+delete"})
SEND_KEYS = frozenset({"enter", "ctrl+enter", "shift+enter", "alt+s"})  # in a messaging or email app
SENDING_KEYS = frozenset({"enter", "ctrl+enter"})  # checked afterwards for whether what was in the box went
RUN_KEYS = frozenset({"enter", "ctrl+enter", "shift+enter", "ctrl+shift+enter"})  # in a launcher
PASTE_KEYS = frozenset({"ctrl+v", "shift+insert", "ctrl+shift+v"})
_MOVES = frozenset({"up", "down", "left", "right", "home", "end", "page_up", "page_down"})  # ``only_moves``
_CHARACTER_KEYS = frozenset(name for name in KEYS if len(name) == 1) | {
    "space", "minus", "equals", "plus", "comma", "period", "slash", "backslash", "semicolon", "quote", "backquote",
    "left_bracket", "right_bracket",
}  # fmt: skip
# Where the keyboard focus can't take text: typing there presses, toggles or picks things instead.
_NOT_TEXT = frozenset(
    {"button", "split button", "menu item", "menu", "menu bar", "check box", "radio button", "link", "tab",
     "tab list", "list item", "tree item", "slider", "scroll bar", "spinner", "header item", "title bar"}
)  # fmt: skip


def _norm(text: str) -> str:
    """Lower case, one space between words, curly apostrophes straight (Windows writes "Don’t save")."""
    return " ".join(text.replace("’", "'").replace("‘", "'").casefold().split())


def _bare(text: str) -> str:
    """A name without the punctuation around it: "Next >" is "next"."""
    return _norm(text).strip(_EDGES)


def _alternatives(phrases: Collection[str], endings: bool = False) -> re.Pattern[str]:
    """Whole words or phrases, longest first; a space in a phrase also matches a hyphen or several spaces."""
    ending = r"(?:s|d|ed|es|ing)?" if endings else ""
    parts = [r"[\s-]+".join(map(re.escape, p.split())) + ending for p in sorted(phrases, key=len, reverse=True)]
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b")


_IN_NAMES = {risk.scope: _alternatives(risk.words) for risk in RISKS if risk.words}
_IN_CONTEXT = {
    risk.scope: _alternatives((*risk.words, *risk.names, *risk.hints), endings=True)
    for risk in RISKS
    if risk.words or risk.names or risk.hints
}


def risk_in_name(name: str, where: Target | None = None) -> Risk | None:
    """The risk a control's name (or the model's label) spells out, if any: "Send" sends, "Don't send" doesn't.

    With ``where``, a web page's own "Run" (an Athena or BigQuery query, a playground's code) isn't an install: it
    runs on that site, not on this PC. "Run anyway" still is (a download's warning), and so is "Run" in a notebook,
    which runs code on this PC for a local Jupyter, and anywhere outside a browser's page."""
    text = _norm(name)
    bare = text.strip(_EDGES)
    on_site = where is not None and _site_runs(where)
    for risk in RISKS:
        if bare in risk.names:
            return risk
        pattern = _IN_NAMES.get(risk.scope)
        if pattern is None:
            continue
        for m in pattern.finditer(text):
            if _NEGATED.search(text[: m.start()]):
                continue
            if on_site and risk.scope == "install" and m.group(0) == "run":
                continue
            return risk
    return None


def _site_runs(t: Target) -> bool:
    """``t`` is in a web page in a browser (not its address bar or a dialog of the browser's own), and not a
    notebook's: a "Run" there runs on the site."""
    if not _processes(t) & BROWSERS or _NOTEBOOK.search(_norm(t.window)):
        return False
    return t.web_page and not _address_bar(t)


def risk_in_context(text: str) -> Risk | None:
    """The risk the text of a dialog speaks of ("Are you sure you want to permanently delete…"), if any."""
    text = _norm(text)
    return next((risk for risk in RISKS if _IN_CONTEXT[risk.scope].search(text)), None)


def _finding(risk: Risk, reason: str = "", where: Target | None = None) -> RiskFinding:
    """``risk`` as the question's finding. Restart and Shut down mean the computer only in Windows' own menus; in
    an app (a browser's "Restart" after an update) they close the app."""
    if risk.scope == "power" and where is not None and not _processes(where) & _WINDOWS_SHELL:
        risk = _RISK["close"]
    return RiskFinding(RiskLevel.CONFIRM, risk.scope, reason or risk.reason)


_commands = itertools.count(1)


def _command(what: str) -> RiskFinding:
    """Input where it can run commands. Its scope is new every time, so a yes covers this one input and never the
    rest of the turn: one approved command mustn't turn into a free shell."""
    return RiskFinding(RiskLevel.CONFIRM, f"command:{_short(what, 60)}#{next(_commands)}", "can run commands")


# What in a command line keeps a question of its own even once the user said yes to working in that terminal
# (``standing_terminal``): removing, moving or overwriting what's there in bulk, stopping processes or the computer,
# changing permissions or the system, running as someone else, fetching code or running it from a string, installing,
# and the git commands that throw work away. Typed words anywhere on the line count, so "echo kill" asks too; the body
# of a here-document (``cat > set.h <<'EOF'`` ... ``EOF``) is the file's text, not commands, and isn't searched.
_RISKY_COMMAND = re.compile(
    r"(?<![\w.-])(?:rm|rmdir|del|erase|unlink|shred|truncate|mv|move|ren|rename|format|mkfs[\w.]*|fdisk|diskpart|"
    r"remove-item|move-item|rename-item|clear-content|clear-recyclebin|"
    r"shutdown|reboot|halt|poweroff|stop-computer|restart-computer|logoff|"
    r"kill|pkill|killall|taskkill|stop-process|scancel|"
    r"reg|regedit|bcdedit|cipher|takeown|icacls|cacls|attrib|chmod|chown|chgrp|setfacl|"
    r"sudo|runas|doas|"
    r"curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod|invoke-expression|iex|start-process|saps|"
    r"set-executionpolicy|new-service|schtasks|crontab|netsh|"
    r"winget|choco|scoop|apt|apt-get|yum|dnf|pacman|brew|npm|npx|pip|pip3|pipx|conda|gem|cargo|msiexec|"
    r"docker|kubectl|terraform|passwd|useradd|userdel|"
    r"git\s+(?:push|reset|clean|checkout|restore|rebase|rm|filter-branch|branch\s+-[dD]|stash\s+(?:drop|clear)))"
    r"(?![\w-])"
    r"|(?<![\w.-])(?:python[\d.]*|py|bash|sh|zsh|powershell|pwsh|cmd)(?:\.exe)?\s+(?:-c|/c|-command|-encodedcommand)\b"
    r"|\|\s*(?:sh|bash|zsh|iex|python[\d.]*|pwsh|powershell)\b"
    r"|>\s*/dev/(?:sd|nvme|hd)",
    re.IGNORECASE,
)
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")
# Keys that run or stop what's already on a terminal's line, and nothing new: Enter runs the line typed (and asked
# about) before, Ctrl+C stops it. A paste is judged by what's on the clipboard.
_TERMINAL_KEYS = frozenset({"enter", "ctrl+c"})


def _without_heredocs(text: str) -> str:
    """``text`` without the bodies of its here-documents: the lines after ``<<'EOF'`` up to the one that's just
    ``EOF``. A here-document left open (the paste ends before its EOF) drops the rest of the text, which is all body."""
    lines, kept, until = text.splitlines(), [], None
    for line in lines:
        if until is not None:
            if line.strip() == until:
                until = None
            continue
        kept.append(line)
        if (doc := _HEREDOC.search(line)) is not None:
            until = doc.group(2)
    return "\n".join(kept)


def risky_command(text: str) -> bool:
    """Whether a command line (or several) does something a standing yes to the terminal doesn't cover."""
    return _RISKY_COMMAND.search(_without_heredocs(text)) is not None


def is_terminal(t: Target) -> bool:
    """A console or terminal app's window: not the Run box, the registry editor or a management console, where a
    click or Enter changes things on its own."""
    return (
        bool(t.hwnd)
        and is_shell(t)
        and not is_run_box(t)
        and not _processes(t) & {"regedit.exe", "mmc.exe"}
        and not t.timed_out
    )


def standing_terminal(finding: RiskFinding | None, t: Target, text: str | None) -> RiskFinding | None:
    """A command in a terminal that the user's yes to working in that terminal covers: once they said yes to one
    input there, the rest of the same task (or of their own turn) types, pastes and presses Enter there without a
    question each time. 85 questions in under an hour (2026-11-04: pasting six files into a cluster session, every
    tab switch, paste and Enter asked) made the questions worthless.

    ``text`` is what the input adds to the command line: the typed text, the clipboard for a paste, "" for Enter or
    a plain click; None when that can't be known (the clipboard holds no text). Anything ``risky_command`` finds keeps
    its own question, and so does everything outside a terminal (the Run box, a launcher)."""
    if finding is None or text is None or not finding.scope.startswith("command:") or not is_terminal(t):
        return finding
    if risky_command(text):
        return finding
    return dataclasses.replace(finding, scope=f"terminal:{t.window_process or t.process}#{t.hwnd}")


def _unchecked(what: str) -> RiskFinding:
    """UI Automation didn't answer in time (a busy or hung app), so what the input would do is unknown: asked, and
    the yes covers this one input only."""
    return RiskFinding(
        RiskLevel.CONFIRM,
        f"unchecked:{_short(what, 60)}#{next(_commands)}",
        "may not be safe: I couldn't check it first",
    )


# --------------------------------------------------------------------------- what kind of place it is


def _processes(t: Target) -> set[str]:
    return {p.casefold() for p in (t.process, t.window_process) if p}


def is_run_box(t: Target) -> bool:
    return t.window_class == "#32770" and _norm(t.window) in RUN_BOXES


def is_shell(t: Target) -> bool:
    """A console, shell or terminal, the Run box, the registry editor or a management console."""
    if _processes(t) & SHELLS or t.window_class in SHELL_CLASSES or is_run_box(t):
        return True
    if _processes(t) & _PYTHON and re.search(r"\bshell\b", _norm(t.window)):
        return True
    # A terminal inside another app says so in its own name or its container's; a page's or window's title
    # ("PowerShell tutorial - Google Chrome") is only what's being shown.
    title = _norm(t.window)
    names = (_norm(name) for kind, name in t.chain if kind not in _TITLED)
    return any(_SHELL_WORDS.search(name) and not title.startswith(name.rstrip("…")) for name in names)


def _address_bar(t: Target) -> bool:
    return t.kind == "text box" and re.search(r"\baddress\b", _norm(t.name)) is not None


def is_launcher(t: Target) -> bool:
    """Where Enter runs what was typed: the Start menu's search, File Explorer's address bar."""
    processes = _processes(t)
    return bool(processes & LAUNCHERS) or ("explorer.exe" in processes and _address_bar(t))


def is_messaging(t: Target) -> bool:
    """A messaging or email app, or a browser showing one (in the page, not its address bar), where Enter sends."""
    processes = _processes(t)
    if processes & MESSAGING:
        return True
    in_page = (t.web_page or not t.described) and not _address_bar(t)
    return bool(processes & BROWSERS) and in_page and _MESSAGING_SITES.search(_norm(t.window)) is not None


def is_email(t: Target) -> bool:
    """An email app, where single keys outside a text field can delete or move messages."""
    processes = _processes(t)
    if processes & EMAIL:
        return True
    return bool(processes & BROWSERS) and _EMAIL_SITES.search(_norm(t.window)) is not None


def _action(t: Target) -> tuple[str, str] | None:
    """(kind, name) of the control a click on ``t`` works: ``t`` itself when it's a button, link or menu item, else
    the one an icon or a text is inside; None when there's none (plain text, a list's row, a picture)."""
    for kind, name in t.chain:
        if kind in ACTION_KINDS:
            return kind, name or t.name
    return None


def _action_name(t: Target) -> str:
    action = _action(t)
    return action[1] if action is not None else ""


def _runs_a_program(t: Target, label: str = "", opened: bool = True) -> bool:
    """Whether opening ``t`` runs code: a program, script or installer (by the item's file name, File Explorer's
    kind of file, or the model's label for it). ``opened``: a double-click or Enter, not just a click on a link."""
    name = next((n for kind, n in t.chain if kind in ACTION_KINDS or kind in _ITEM_KINDS), t.name)
    files = _RUNNABLE_FILE if opened else _PROGRAM
    if any(files.search(_norm(text)) for text in (name, label) if text):
        return True
    return opened and _RUNNABLE_TYPE.search(_norm(t.item_type)) is not None


def _generic_risk(t: Target) -> RiskFinding | None:
    """An OK, Yes or Next button does whatever the dialog (or form) around it says."""
    if t.container in ("dialog", "form") and (risk := risk_in_context(t.container_text)) is not None:
        return _finding(risk)
    if t.secondary and (risk := risk_in_context(t.window)) is not None:
        return _finding(risk)
    if any(_INSTALLER.search(process) for process in _processes(t)):
        return _finding(_RISK["install"])
    return None


def _closes(kind: str, name: str, t: Target) -> bool:
    """An app's own close button (not a dialog's), or a Close menu item (the window menu's closes the app)."""
    return _bare(name) == "close" and (kind == "menu item" or (t.caption and not t.secondary))


def _control_risk(t: Target) -> RiskFinding | None:
    """What clicking the control ``t`` (or pressing Enter or Space on it) does, if that's risky."""
    action = _action(t)
    if action is None:
        return None
    kind, name = action
    bare = _bare(name)
    if _closes(kind, name, t):
        return _finding(_RISK["close"])
    if (risk := risk_in_name(name, where=t)) is not None:
        return _finding(risk, where=t)
    if bare in GENERIC:
        return _generic_risk(t)
    if bare == "no" and t.container == "dialog" and _SAVE_PROMPT.search(_norm(t.container_text)):
        return _finding(_RISK["delete"], "throws away unsaved changes")
    return None


def _around_risk(t: Target) -> RiskFinding | None:
    """Enter in a text field presses a button of the form or dialog it's in, and a letter outside one can be any
    item's access key: judged by all the buttons and menu items around it."""
    if t.container not in ("dialog", "form", "menu"):
        return None
    for kind, name in t.actions:
        if _closes(kind, name, t):
            return _finding(_RISK["close"])
        if (risk := risk_in_name(name, where=t)) is not None:
            return _finding(risk, where=t)
    if any(_bare(name) in GENERIC for _, name in t.actions):
        return _generic_risk(t)
    return None


def _switches_tab(t: Target) -> bool:
    """A click on a terminal's tab itself (its title or icon): it picks which session is in front and types nothing
    into one. A button on the tab (its close button) or in the tab strip (New Tab) isn't the tab."""
    for kind, _ in t.chain or ((t.kind, t.name),):
        if kind == "tab":
            return True
        if kind in ACTION_KINDS:
            return False
    return False


def classify_click(
    t: Target, label: str = "", risky: bool = False, button: str = "left", double: bool = False
) -> RiskFinding | None:
    """Whether a click on ``t`` needs the user's yes, and why. ``label`` and ``risky`` are the model's word for it:
    either can make a click risky, never less risky."""
    if is_shell(t):
        if button == "left" and not double and is_terminal(t) and _switches_tab(t):
            return _control_risk(t)  # switching tabs runs nothing; a tab's own close button is asked as anywhere
        return _command(f"{button} click")  # a right click pastes into a console, and Enter could follow
    if t.timed_out:
        return _unchecked(f"{button} click on {label or 'it'}")
    if (found := _control_risk(t)) is not None:
        return found
    # A double-click opens what it's on, and so does a click on a button or link (a browser's downloaded file).
    if (double or _action(t) is not None) and _runs_a_program(t, label, opened=double):
        return _finding(_RISK["install"], "runs a program")
    if label and (risk := risk_in_name(label, where=t)) is not None:
        return _finding(risk, where=t)
    if risky:
        what = _bare(label or _action_name(t) or t.name)
        return RiskFinding(RiskLevel.CONFIRM, f"flagged:{what}", "can't easily be undone")
    return None


def classify_typing(t: Target, text: str, enter: bool) -> RiskFinding | None:
    """Whether typing ``text`` into ``t`` (the keyboard focus), then maybe Enter, needs the user's yes, and why."""
    submits = enter or "\n" in text  # a line break is an Enter too
    if is_shell(t) or (submits and is_launcher(t)):
        return _command(text + (" + enter" if enter else ""))
    if t.password:
        return RiskFinding(RiskLevel.CONFIRM, "password", "enters a password")
    if submits and is_messaging(t):
        return RiskFinding(RiskLevel.CONFIRM, "send", "sends a message")
    if t.timed_out:
        return _unchecked("typing")  # a password box or a form's Send can't be told apart from the window alone
    if t.described and not t.editable:
        # Letters outside a text field (where it can't be told whether that's one) are keys: a site's shortcuts
        # in an email app, a dialog's or menu's access keys.
        if is_email(t):
            return _finding(_RISK["delete"], "can delete or move messages")
        return _around_risk(t)
    return _around_risk(t) if submits else None


def only_moves(combo: KeyCombo) -> bool:
    """Whether ``combo`` only moves the caret or the selection: Ctrl+A, the arrows, Home and End, Page Up and Down,
    with Shift to select and Ctrl to go a word or to the very start or end. Nothing is typed, run, sent or deleted,
    even in a terminal (selecting a console's command to type over it asked every time)."""
    return combo.text == "ctrl+a" or (combo.key in _MOVES and set(combo.modifiers) <= {"ctrl", "shift"})


def classify_keys(t: Target, combo: KeyCombo) -> RiskFinding | None:
    """Whether pressing ``combo`` with the keyboard focus on ``t`` needs the user's yes, and why."""
    keys = combo.text
    if only_moves(combo):
        return None
    if keys in COMMAND_KEYS or is_shell(t) or (keys in RUN_KEYS and is_launcher(t)):
        return _command(keys)
    if keys in RUN_CELL_KEYS and _NOTEBOOK.search(_norm(t.window)):
        return _command(keys)  # a notebook runs the cell: code on this PC for a local Jupyter
    if keys in CLOSE_KEYS:
        return _finding(_RISK["close"])
    if keys in DELETE_KEYS or (keys == "ctrl+d" and is_email(t)):
        return _finding(_RISK["delete"])
    writes = combo.key in _CHARACTER_KEYS and not {"ctrl", "win"} & set(combo.modifiers)
    if t.password and ((writes and "alt" not in combo.modifiers) or keys in PASTE_KEYS):
        return RiskFinding(RiskLevel.CONFIRM, "password", "enters a password")
    # Enter sends from the message box (or wherever the focus is when that can't be told); Alt+S is the Send
    # button's own key in an email window, wherever the focus is in it.
    typing_here = t.editable or not t.described
    if is_messaging(t) and (keys == "alt+s" or (keys in SEND_KEYS and typing_here)):
        return RiskFinding(RiskLevel.CONFIRM, "send", "sends a message")
    if t.timed_out and (writes or keys in ("enter", "space", "ctrl+enter", "shift+enter")):
        return _unchecked(keys)  # what they'd press or type into is unknown; moving around (arrows, pages) is fine
    if keys in RUN_KEYS and not t.editable and _runs_a_program(t):
        return _finding(_RISK["install"], "runs a program")  # Enter opens the selected file
    if keys in ("enter", "space") and not t.editable:
        return _control_risk(t)  # presses the focused button
    if keys == "enter":
        return _around_risk(t)
    if writes and not t.editable:
        if is_email(t):
            return _finding(_RISK["delete"], "can delete or move messages")  # Gmail's #, Outlook's single keys
        return _around_risk(t)
    return None


def pdf_viewer(t: Target) -> bool:
    """Whether ``t`` is a PDF's page in the browser's own viewer. Its form fields aren't described to UI Automation,
    so the keyboard focus in one looks like the page itself; and the viewer has no single-key shortcuts, so typing
    there goes into the field clicked, or nowhere."""
    if not t.page_root or (t.process or t.window_process).casefold() not in BROWSERS:
        return False
    return bool(_PDF_FILE.search(t.name.strip()) and _PDF_WINDOW.search(" ".join(t.window.split())))


def clicked_into_editor(t: Target, clicked: Target | None) -> bool:
    """Whether the focus ``t``, a web page itself, is in fact the code editor just clicked in that page: CodeMirror's
    text surface (Snowflake's worksheets) is a contenteditable that browsers may not report as the keyboard focus,
    so after a click into it the focus looks like the page. ``clicked``: what her last click this turn landed on."""
    if clicked is None or not t.page_root or (clicked.hwnd, clicked.pid) != (t.hwnd, t.pid):
        return False
    return clicked.web_page and (clicked.code_editor or any(kind == "text box" for kind, _ in clicked.chain))


# What a web page's element is called when UI Automation sees nothing inside it ("" when it can't say at all).
_SURFACE_KINDS = frozenset({"group", "pane", "image", "custom", ""})


def drawn_surface(t: Target) -> bool:
    """Whether ``t`` is a web page's surface whose contents are drawn, not described: a remote screen streamed into
    the page (a cloud workspace beside a course's instructions), a canvas, a video. UI Automation sees one unnamed
    element there and can't tell what a click or typing did inside, so the screen itself is checked (``_landing``)."""
    return t.web_page and not t.page_root and not t.editable and not t.name and t.kind in _SURFACE_KINDS


def clicked_into_surface(t: Target, clicked: Target | None) -> bool:
    """Whether the focus ``t``, a web page itself, is in fact in the drawn surface her last click landed in this turn
    (``clicked``): the page keeps UI Automation's focus whatever has the keys inside a streamed screen."""
    if clicked is None or not t.page_root or (clicked.hwnd, clicked.pid) != (t.hwnd, t.pid):
        return False
    return drawn_surface(clicked)


def typing_problem(t: Target, clicked: Target | None = None) -> str | None:
    """Why typing now would go somewhere other than a text field, or None. ``clicked``: what her last click this turn
    landed on, if anything."""
    if not t.hwnd:
        return "Nothing has the keyboard focus. Click the text field first."
    if (
        t.page_root
        and not pdf_viewer(t)
        and not clicked_into_editor(t, clicked)
        and not clicked_into_surface(t, clicked)
    ):
        return (
            f"The keyboard focus is on the page itself in {app_name(t)}, not in a text field, and typing there can "
            "set off the site's own shortcuts. Click the text field first (inside a streamed or embedded screen too: "
            "a click that lands in it lets typing go there)."
        )
    if t.kind in _NOT_TEXT:
        return f"The keyboard focus is on {thing(t)} in {app_name(t)}, not in a text field. Click the text field first."
    return None


# --------------------------------------------------------------------------- naming things for people


_FRIENDLY = {
    "cmd.exe": "Command Prompt",
    "powershell.exe": "PowerShell",
    "pwsh.exe": "PowerShell",
    "windowsterminal.exe": "Windows Terminal",
    "openconsole.exe": "Windows Terminal",
    "conhost.exe": "the console",
    "explorer.exe": "File Explorer",
    "searchhost.exe": "Windows Search",
    "searchapp.exe": "Windows Search",
    "startmenuexperiencehost.exe": "the Start menu",
    "systemsettings.exe": "Settings",
    "regedit.exe": "Registry Editor",
    "powerpnt.exe": "Microsoft PowerPoint",
    "winword.exe": "Microsoft Word",
    "excel.exe": "Microsoft Excel",
    "onenote.exe": "Microsoft OneNote",
    "mmc.exe": "a management console",
    "taskmgr.exe": "Task Manager",
}


def app_name(t: Target) -> str:
    """What to call the app ``t`` is in: "Discord", "the taskbar", "the Run box"; "" when nothing is known."""
    if t.window_class in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
        return "the taskbar"
    if t.window_class in ("Progman", "WorkerW"):
        return "the desktop"
    if is_run_box(t):
        return "the Run box"
    process = t.process or t.window_process
    if process.casefold() in _FRIENDLY:
        return _FRIENDLY[process.casefold()]
    if t.app:
        return t.app
    if process:
        return process[:-4] if process.casefold().endswith(".exe") else process
    return t.window


def _short(text: str, limit: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def thing(t: Target) -> str:
    """How to name what's at a spot for the model: 'the button “Send”', 'the text “Liked Songs”', 'that spot'."""
    kind, name = _action(t) or (t.kind, t.name)
    if name:
        return f"the {kind} “{_short(name)}”" if kind else f"“{_short(name)}”"
    return f"the {kind}" if kind else "that spot"


def _preview(text: str, limit: int = 60) -> str:
    return _short(text, limit)


# --------------------------------------------------------------------------- the turn and the screen


@dataclass(frozen=True)
class Spot:
    """A place on the desktop to click or scroll: physical pixels, the thing's extent and name when known."""

    x: int
    y: int
    box: Rect | None = None
    name: str = ""


def current_turn(ctx: ToolContext) -> TurnInfo | None:
    """The user's turn that's running now; None between turns, or when the app can't say (tests, some chats)."""
    get = ctx.extras.get("turn")
    if not callable(get):
        return None
    try:
        turn = get()
    except Exception:
        log.warning("Asking for the running turn failed", exc_info=True)
        return None
    return turn if isinstance(turn, TurnInfo) else None


def heard_talking(ctx: ToolContext) -> float | None:
    """When (``time.monotonic()``) the user was last heard talking while she worked in the running turn
    (``ctx.extras["heard_talking"]``); None when they weren't, or when the app can't say (a screen task, tests)."""
    get = ctx.extras.get("heard_talking")
    if not callable(get):
        return None
    try:
        heard = get()
    except Exception:
        log.warning("Asking whether the user is talking failed", exc_info=True)
        return None
    return float(heard) if isinstance(heard, int | float) else None


async def _pins_aside(ctx: ToolContext) -> bool:
    """Pinned panels go out of the way of a click they stopped (``PanelHost.step_aside``). Whether any did."""
    host = ctx.extras.get("visuals")
    step_aside = getattr(host, "step_aside", None)
    if step_aside is None:
        return False
    try:
        return bool(await step_aside(ctx.extras.get("screen_busy")))
    except Exception:
        log.warning("Pinned panels couldn't step aside", exc_info=True)
        return False


def _screen_busy(ctx: ToolContext) -> bool:
    """Whether a screen task has the mouse and keyboard (``ctx.extras["screen_busy"]``, only in the conversation's
    own context: the task's tools never see it)."""
    busy = ctx.extras.get("screen_busy")
    try:
        return callable(busy) and busy() is True
    except Exception:
        log.warning("Asking whether a screen task runs failed", exc_info=True)
        return False


@dataclass(frozen=True)
class Inspection:
    """What ``inspect_risk`` saw for one call, kept on its arguments for ``describe_call`` and ``run``: the click
    goes exactly where the risk check looked, even if a newer screenshot came in meanwhile."""

    target: Target
    finding: RiskFinding | None
    spot: Spot | None = None


class Inspector(Protocol):
    """What's at a spot, or has the keyboard focus, as the risk check sees it."""

    async def point(self, x: int, y: int, ignore_pids: frozenset[int]) -> Target: ...

    async def focus(self, ignore_pids: frozenset[int]) -> Target: ...

    async def focus_id(self, ignore_pids: frozenset[int]) -> tuple[int, ...] | None: ...


class UIAInspector:
    """From UI Automation and Windows (``_uia``), on the UI Automation thread."""

    async def point(self, x: int, y: int, ignore_pids: frozenset[int]) -> Target:
        return await _uia.inspect_point(x, y, ignore_pids)

    async def focus(self, ignore_pids: frozenset[int]) -> Target:
        return await _uia.inspect_focus(ignore_pids)

    async def focus_id(self, ignore_pids: frozenset[int]) -> tuple[int, ...] | None:
        return await _uia.focus_now(ignore_pids)

    async def box_text(self, ignore_pids: frozenset[int]) -> str | None:
        return await _uia.read_focus_value(ignore_pids)


def _boxed(text: str) -> str:
    """``text`` as compared in a message box: invisible marks out, spaces and line breaks one space, any case."""
    return " ".join(text.translate(_INVISIBLE).split()).casefold()


def checks_send(t: Target) -> bool:
    """Whether Enter in ``t`` is worth checking for a send: a text box or a web page's editor in a messaging app or
    an AI chat (Claude, ChatGPT), where Enter sends. Not a search box, an address bar, a document, a code editor or a
    terminal, where the text stays or Enter is a new line or a command."""
    if t.code_editor or is_shell(t) or _address_bar(t) or not (t.kind == "text box" or (t.web_page and t.editable)):
        return False
    if _SEARCH_BOX.search(_norm(t.name)):
        return False
    return _is_chat(t) or is_messaging(t)


def _is_chat(t: Target) -> bool:
    """An AI chat app (Claude, ChatGPT), or a browser showing one, where Enter sends."""
    processes = _processes(t)
    return bool(processes & CHAT_APPS) or (bool(processes & BROWSERS) and bool(_CHAT_SITES.search(_norm(t.window))))


def enter_sends(t: Target) -> bool:
    """Where Enter sends what's typed so far (a messaging app, an AI chat), so a line break must be Shift+Enter."""
    return is_messaging(t) or (_is_chat(t) and not _address_bar(t))


class _SpotArgs(ToolArgs):
    """Where on the screen, exactly as point_at_screen takes it: a pixel of the latest screenshot or zoom, or a
    find_on_screen match."""

    x: int | None = Field(default=None, description="Pixels from the left edge of 'image'.")
    y: int | None = Field(default=None, description="Pixels from the top edge of 'image'.")
    image: Literal["screenshot", "zoom"] = Field(
        default="screenshot", description="x and y are in the latest look_at_screen or zoom_screen image."
    )
    element: int | None = Field(default=None, ge=1, description="A find_on_screen match number, instead of x and y.")

    def _check_spot(self, required: bool) -> None:
        has_xy = self.x is not None or self.y is not None
        if self.element is not None and has_xy:
            raise ValueError("give either x and y, or element, not both")
        if has_xy and (self.x is None or self.y is None):
            raise ValueError("give both x and y")
        if required and self.element is None and not has_xy:
            raise ValueError("give x and y, or an element number from find_on_screen")

    @property
    def has_spot(self) -> bool:
        return self.element is not None or self.x is not None


def _pointing_box(t: Target | None) -> Rect | None:
    """The thing's rectangle for the avatar to stand clear of, when it's the size of a control (not a whole pane)."""
    if t is None or t.rect is None:
        return None
    left, top, right, bottom = t.rect
    return t.rect if 0 < right - left <= 600 and 0 < bottom - top <= 300 else None


def _own_hotkey(ctx: ToolContext, combo: KeyCombo) -> str | None:
    """Why ``combo`` is the assistant's own talk or stop hotkey, which would interrupt her; None when it isn't."""
    hotkeys = getattr(ctx.config, "hotkeys", None)
    if hotkeys is None or not hotkeys.enabled or combo.key is None:
        return None
    bits = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
    mine = (sum(bits[m] for m in combo.modifiers), KEYS[combo.key][0])
    for which in ("talk", "stop"):
        try:
            mods, vk = parse_hotkey(getattr(hotkeys, which) or "")
        except ValueError:
            continue  # "" turns one off
        if (mods & ~MOD_NOREPEAT, vk) == mine:
            return f"{combo.label} is the assistant's own {which} hotkey, so I won't press it."
    return None


def _placed(finding: RiskFinding | None, t: Target) -> RiskFinding | None:
    """A send (Submit, Send, Post) marked with its window, for a standing approval to submit there
    (``Assistant.requested``); other findings as they are."""
    if finding is None or finding.scope != "send" or not t.hwnd:
        return finding
    return dataclasses.replace(finding, place=f"{t.window_process or t.process}#{t.hwnd}", window=t.window)


_STEP_SCOPES = frozenset({"send", "install", "irreversible"})


def _site_step(finding: RiskFinding | None, t: Target, ctx: ToolContext) -> RiskFinding | None:
    """A Submit Answer, Continue or Run Code in a web page of a site the user lets her submit on (DataCamp,
    ``submit_sites``) is the submit it looks like, whatever its surroundings or the model's own ``risky`` made of it:
    a send, which the confirmers then wave through (or not: hints, places that always ask). Never a buy, delete,
    password or command there, nor a spot UI Automation couldn't check, nor the browser's own windows."""
    if finding is None or t.timed_out or t.secondary or not _processes(t) & BROWSERS or _address_bar(t):
        return finding
    if finding.scope not in _STEP_SCOPES and not finding.scope.startswith("flagged:"):
        return finding
    if not is_step_button(_action_name(t) or t.name):
        return finding
    if not on_submit_site(t.window, ctx.config.permissions.submit_without_asking):
        return finding
    return _finding(_RISK["send"])


def _free_step(t: Target, ctx: ToolContext, found: RiskFinding | None) -> bool:
    """A step button that needs nobody's yes (Submit Answer, Continue... on DataCamp, ``submit_sites.free_step``):
    the spot turning into one between the check and the click (the code typed, the Submit Answer appearing) is no
    reason to stop and refuse it. Same limits as ``_site_step``: a browser's web page, checked by UI Automation, and
    only when what's risky about it (``found``) is the submit it is: a buy, a password or a delete still stops it, and
    so does any of it in a turn texted from their phone (they can't see the screen)."""
    if t.timed_out or t.secondary or not _processes(t) & BROWSERS or _address_bar(t):
        return False
    if found is not None and ((step := _site_step(found, t, ctx)) is None or step.scope != "send"):
        return False
    if (turn := current_turn(ctx)) is not None and turn.remote:
        return False
    permissions = ctx.config.permissions
    return free_step(
        t.window,
        _action_name(t) or t.name,
        permissions.submit_without_asking,
        permissions.always_ask_before_sending,
    )


def _free_change(now: Target, seen: Inspection, args: ClickArgs, ctx: ToolContext) -> bool:
    """The spot changed between the check and the click into a step button that needs no yes (``_free_step``): only
    the page's own content changing in the same window (the text being typed turning into the Submit Answer), not a
    dialog coming up over it, another window, the page's bare surface first seen, nothing the user was asked about,
    and nothing named otherwise than the click was asked for."""
    before = seen.target
    if seen.finding is not None or now.container == "dialog" or now.hwnd != before.hwnd:
        return False
    if not before.web_page or before.kind in ("pane", "group", "document", "") or not now.web_page:
        return False
    if args.label and args.label.casefold() not in (_action_name(now) or now.name).casefold():
        return False
    return _free_step(now, ctx, _click_risk(now, args, ctx))


def _editing_answer(t: Target, ctx: ToolContext) -> bool:
    """The keyboard focus is in a text box of a web page on a site she submits on without asking (DataCamp's code
    editor): Delete there only takes out text she typed or selected, which Ctrl+Z brings back."""
    if not t.editable or t.timed_out or t.secondary or t.password or not _processes(t) & BROWSERS or _address_bar(t):
        return False
    if (turn := current_turn(ctx)) is not None and turn.remote:
        return False  # texted from their phone: they can't see what goes
    permissions = ctx.config.permissions
    return on_submit_site(t.window, permissions.submit_without_asking) and not on_submit_site(
        t.window, permissions.always_ask_before_sending, trusted=False
    )


def _needs_yes(finding: RiskFinding) -> str:
    return f"That needs the user's yes first (it {finding.reason}), and nobody asked them, so I didn't do it."


_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


def _one_at_a_time() -> asyncio.Lock:
    """The lock all control tools share on the running event loop (an asyncio lock belongs to one loop)."""
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = _locks[loop] = asyncio.Lock()
    return lock


# What her last click in a turn landed on and where, by the watch the tools share: typing then knows a click into a
# web code editor or a streamed screen put the keyboard there, though the focus looks like the page itself
# (``clicked_into_editor``, ``clicked_into_surface``).
_last_clicks: weakref.WeakKeyDictionary[Watch, tuple[int, Target, tuple[int, int]]] = weakref.WeakKeyDictionary()


# --------------------------------------------------------------------------- the tools


class _Control(Tool):
    """What all four share: only in a running turn, stopping when the user takes over, the screen memory."""

    risk = RiskLevel.SAFE  # raised by inspect_risk when what's on screen says the call is risky
    category = "system"

    def __init__(
        self,
        watch: Watch | None = None,
        inspector: Inspector | None = None,
        memory: ScreenMemory | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        before: InputSnapshots | None = None,
        landing: _landing.Landing | None = None,
        delivery: _landing.Delivery | None = None,
        clip: Any = None,
    ) -> None:
        self.watch = watch if watch is not None else _input.WATCH
        self.inspector = inspector if inspector is not None else UIAInspector()
        self.memory = memory if memory is not None else SCREEN
        self.sleep = sleep or asyncio.sleep
        self._real = watch is None  # the real mouse and keyboard, which only exist on Windows
        # The copy of the window right before her input, for wait_for_screen_change: of the real screen only with the
        # real mouse and keyboard, so tests with fakes never capture it.
        self.before = before if before is not None else (BEFORE if self._real else None)
        # Whether input into a drawn surface showed on the screen (the real screen likewise only with the real mouse
        # and keyboard), which way of getting it in worked where, and the clipboard a paste goes through.
        self.landing = landing if landing is not None else _landing.Landing(sleep=self.sleep, real=self._real)
        self.delivery = delivery if delivery is not None else (_landing.DELIVERY if self._real else _landing.Delivery())
        self.clip = clip if clip is not None else clipboard

    @property
    def desktop(self) -> Any:
        return self.watch.desktop

    def _refusal(self, ctx: ToolContext) -> str | None:
        """Why no control tool may act right now, or None."""
        if self._real and sys.platform != "win32":
            return NOT_WINDOWS
        turn = current_turn(ctx)
        if turn is None:
            return NOT_IN_TURN
        if turn.interrupted:
            return INTERRUPTED
        if _screen_busy(ctx):
            return SCREEN_BUSY
        if self.watch.taken_over(turn.id):
            return TAKEN_OVER
        return None

    async def precheck(self, args: Any, ctx: ToolContext) -> str | None:
        return self._refusal(ctx)

    async def run(self, args: Any, ctx: ToolContext) -> ToolResult:
        # One action at a time: two calls the model made at once mustn't mix their moves and keys.
        async with _one_at_a_time():
            return await self._act(args, ctx)

    async def _act(self, args: Any, ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    def _start(self, ctx: ToolContext) -> tuple[TurnInfo, None] | tuple[None, str]:
        if problem := self._refusal(ctx):
            return None, problem
        turn = current_turn(ctx)
        if turn is None:  # it just ended
            return None, NOT_IN_TURN
        # Typing "y" or clicking into the chat to answer one of the assistant's questions isn't taking over.
        self.watch.listen(ctx.events, PermissionResolved, (ASKED_FOR, ON_ITS_OWN, ALREADY_APPROVED))
        # Before her first input of the turn, the user's mouse is still theirs: the baseline starts with this call.
        self.watch.begin(turn.id)
        return turn, None

    def _stop(self, ctx: ToolContext, turn: TurnInfo) -> str | None:
        """Why to stop before the next step (the turn is over or cut off, the user took over), or None."""
        latest = current_turn(ctx)
        if latest is None or latest.id != turn.id:
            return NOT_IN_TURN
        if latest.interrupted:
            return INTERRUPTED
        if _screen_busy(ctx):
            return SCREEN_BUSY
        try:
            if not self.watch.check(turn.id, avatar_pids(ctx)):
                return TAKEN_OVER
        except OSError as exc:
            return f"I couldn't read the mouse and keyboard ({exc}), so I stopped."
        return None

    def _locate(self, args: _SpotArgs) -> Spot | str:
        """The desktop spot the arguments name, or why there isn't one (for the model)."""
        if args.element is not None:
            matches = self.memory.matches()
            if not matches:
                return "I have no recent find_on_screen results. Search again first."
            if args.element > len(matches):
                return f"There's no match {args.element}; pick 1 to {len(matches)}."
            match = matches[args.element - 1]
            if match.covered:
                return (
                    f"Match {args.element} is behind another window, so the click would land on that window. "
                    "Bring its window to the front first."
                )
            x, y = match.centre
            return Spot(x, y, match.rect, match.name)
        record = self.memory.shot() if args.image == "screenshot" else self.memory.zoom()
        if record is None:
            return NO_SHOT if args.image == "screenshot" else "I don't have a recent zoom. Use zoom_screen first."
        age = self.memory.now() - record.taken
        if age > MAX_SHOT_AGE_S:
            return f"My last look at the screen is {age:.0f} seconds old, so things may have moved. Look again first."
        assert args.x is not None and args.y is not None  # the arguments' validator made sure
        if not (0 <= args.x < record.width and 0 <= args.y < record.height):
            return f"That spot is outside the {args.image}, which is {record.width}x{record.height} pixels."
        x, y = record.to_screen(args.x, args.y)
        return Spot(x, y)

    async def _copy_before(self, action: str) -> None:
        """Copy the window right before her input, for wait_for_screen_change to compare with."""
        if self.before is not None:
            await self.before.take(action)

    def _clicked(self, turn: TurnInfo | None) -> Target | None:
        """What her last left click in ``turn`` landed on; None when she hasn't clicked in it."""
        last = _last_clicks.get(self.watch)
        return last[1] if turn is not None and last is not None and last[0] == turn.id else None

    def _clicked_at(self, turn: TurnInfo | None) -> tuple[int, int] | None:
        """Where her last left click in ``turn`` went; None when she hasn't clicked in it."""
        last = _last_clicks.get(self.watch)
        return last[2] if turn is not None and last is not None and last[0] == turn.id else None

    async def _focus_back(self, now: Target, seen: Inspection | None, ctx: ToolContext) -> Target:
        """What has the keyboard focus: ``now``, or, when that's one of her own windows (her chat window, where the
        user just answered her question about this very step) and the window she checked (``seen``) is still there,
        what has it once that window has been given the keys back. The answer in her window was no move of the
        user's away from the task."""
        if seen is None or not seen.target.hwnd or (now.hwnd == seen.target.hwnd and now.same_thing(seen.target)):
            return now
        pids = avatar_pids(ctx)
        front = getattr(self.desktop, "foreground_pid", None)
        give = getattr(self.desktop, "bring_to_front", None)
        if not pids or front is None or give is None:
            return now
        try:
            if await asyncio.to_thread(front) not in pids or not await asyncio.to_thread(give, seen.target.hwnd):
                return now
        except OSError:
            return now
        await self.sleep(FOCUS_BACK_S)
        log.info("The keys were in her own window: gave them back to %s", app_name(seen.target) or "the window")
        return await self.inspector.focus(pids)

    def _focus_moved(self, target: Target) -> str | None:
        if target.hwnd and self.desktop.foreground() != target.hwnd:
            return "The active window changed, so I stopped."
        return None

    async def _box_text(self, pids: frozenset[int]) -> str | None:
        """What's in the box that has the keyboard focus, or None when it can't be told."""
        read = getattr(self.inspector, "box_text", None)
        if read is None:
            return None
        try:
            return await asyncio.wait_for(read(pids), BEFORE_LOOK_S)
        except Exception:
            log.debug("Reading the focused box failed", exc_info=True)
            return None

    async def _before_enter(self, pids: frozenset[int]) -> tuple[str | None, tuple[int, ...] | None]:
        """What's in the focused box right before Enter, and which element it is."""
        return await self._box_text(pids), await self.inspector.focus_id(pids)

    async def _sent(
        self, typed: str, before: tuple[str | None, tuple[int, ...] | None], focus: Target, pids: frozenset[int]
    ) -> str:
        """What Enter did with what was in the box (``claims.SENT_*``): ``before`` is what it held right before
        Enter, ``typed`` what she typed (its end is looked for; what was there, when she typed nothing), ``focus``
        where the keyboard was. The text gone from it is a send; still there after the waits, it didn't go; a box
        that doesn't say is told by the window or the focus moving on (a form that went), or not at all."""
        before, box = before
        tail = _boxed(typed or before or "")[-SENT_TAIL_CHARS:].strip()
        if not tail or before is None or tail not in _boxed(before):
            return await self._moved_on(focus, pids)
        for wait in SENT_WAITS_S:
            await self.sleep(wait)
            if box is not None and (now := await self.inspector.focus_id(pids)) is not None and now != box:
                return SENT_WENT  # the focus left the box: what it reads now is something else
            after = await self._box_text(pids)
            if after is None:  # the box went away, or the focus left it
                return await self._moved_on(focus, pids)
            if tail not in _boxed(after):
                return SENT_WENT
        return SENT_STUCK

    async def _moved_on(self, focus: Target, pids: frozenset[int]) -> str:
        """``SENT_WENT`` when the window or the keyboard focus moved on from ``focus`` since Enter, else unknown."""
        now = await self._look(self.inspector.focus(pids))
        if now.timed_out or focus.timed_out or not focus.hwnd:
            return SENT_UNKNOWN
        # Not the window's title: an unread count in it ("(1) Discord") changes when anyone's message comes in.
        if now.hwnd != focus.hwnd or not now.same_thing(focus):
            return SENT_WENT
        return SENT_UNKNOWN

    @staticmethod
    async def _look(inspecting: Awaitable[Target], timeout_s: float = BEFORE_LOOK_S) -> Target:
        """A look only to report what an action did: one that fails or is slow is unknown (``timed_out``), never an
        error, since the action itself may already have gone in."""
        try:
            return await asyncio.wait_for(inspecting, timeout_s)
        except Exception:
            log.warning("Looking at what an action did failed", exc_info=True)
            return Target(timed_out=True)

    def _avatar_under(self, x: int, y: int, pids: frozenset[int]) -> bool:
        """Whether her own window would take input at (x, y): the mouse is on her there."""
        return bool(pids) and self.desktop.owner_at(x, y) in pids

    def _press(self, ctx: ToolContext) -> None:
        """Have the avatar jab at what she's pointing at, as the click goes in (only a look: it moves nothing)."""
        avatar = pointing_avatar(ctx)
        if avatar is None:
            return
        try:
            avatar.press()
        except Exception:
            log.warning("Asking the avatar to press failed", exc_info=True)

    def _point(self, ctx: ToolContext, spot: Spot, box: Rect | None, label: str) -> bool:
        """Have the avatar point at ``spot``: True once she's on her way."""
        avatar = pointing_avatar(ctx)
        if avatar is None:
            return False
        try:
            return avatar.point(spot.x, spot.y, box=box, label=label) is None
        except Exception:
            log.warning("Asking the avatar to point failed", exc_info=True)
            return False


# ------------------------------------------------------------------ click


class ClickArgs(_SpotArgs):
    button: Literal["left", "right", "middle"] = "left"
    double: bool = Field(default=False, description="Double-click, e.g. to open a file.")
    label: str = Field(default="", description="What it is, briefly: 'the Send button'.")
    risky: bool = Field(
        default=False,
        description="True when you know it sends, buys, deletes, installs, grants access or can't be undone.",
    )
    _inspection: Inspection | None = PrivateAttr(default=None)

    @field_validator("label")
    @classmethod
    def _short_label(cls, value: str) -> str:
        return value[:LABEL_CHARS].strip()  # a long label isn't worth failing the call over

    @model_validator(mode="after")
    def _one_target(self) -> Self:
        self._check_spot(required=True)
        return self


def _click_risk(t: Target, args: ClickArgs, ctx: ToolContext) -> RiskFinding | None:
    return _trust(classify_click(t, args.label, args.risky, args.button, args.double), t, ctx)


def _trust(found: RiskFinding | None, t: Target, ctx: ToolContext) -> RiskFinding | None:
    """No question for deleting something inside a document in an app the user trusts her with
    (``permissions.trusted_apps``: a shape in PowerPoint, a cell in Excel), which Ctrl+Z takes back. Never in one of
    its dialogs ("Don't Save", "Delete this file?"), and never anything else: sending, buying, installing, commands,
    passwords and closing the app are asked there as anywhere. Nor in a turn texted from the user's phone
    (``TurnInfo.remote``): they can't see what's being deleted."""
    if found is None or found.scope != "delete" or t.container == "dialog" or t.timed_out:
        return found
    if (turn := current_turn(ctx)) is not None and turn.remote:
        return found
    if _processes(t) & {"explorer.exe"}:
        return found  # deleting files is always asked, whatever app is trusted
    trusted = [a.casefold() for a in ctx.config.permissions.trusted_apps if a.strip()]
    names = " ".join((app_name(t), t.process, t.window_process)).casefold()
    return None if any(a in names for a in trusted) else found


def graded_refusal(t: Target | None, clicking: bool = False) -> str | None:
    """Why she won't act on ``t`` because it's graded work the user has to do themselves (``core.graded_work``): a
    locked exam browser, or (``clicking``: a click, or Enter or Space on it) a button that turns in a quiz or exam.
    None otherwise."""
    if t is None:
        return None
    if graded_work.locked_exam(t.process) or graded_work.locked_exam(t.window_process):
        return graded_work.LOCKED_EXAM
    if clicking and any(graded_work.ends_attempt(n, t.window) for n in (t.name, _action_name(t)) if n):
        return graded_work.ENDS_ATTEMPT
    return None


def _presses(combo: KeyCombo) -> bool:
    """Whether ``combo`` presses the focused button, as a click would: Enter or Space on their own."""
    return not combo.modifiers and combo.key in ("enter", "space")


def _name_of(t: Target) -> str:
    """The short name of what a click on ``t`` works, for the question and the avatar's label."""
    return _short(_action_name(t) or t.name)


_SURFACES = frozenset({"pane", "group", "document", "image", "text"})


def _same_surface(a: Target, b: Target) -> bool:
    """Whether ``a`` and ``b`` are both plain surface in the same window of the same app, with no button, link or
    menu item under them: a web game's board (checkers on a canvas) is reported as a pane at one look and a group at
    the next, or with a different label, while nothing on it changed. What such a spot now is still gets its own risk
    check."""
    return (
        a.kind in _SURFACES
        and b.kind in _SURFACES
        and (a.hwnd, a.pid) == (b.hwnd, b.pid)
        and _action(a) is None
        and _action(b) is None
    )


def _same_control(a: Target, b: Target) -> bool:
    """Whether a click on ``a`` and on ``b`` works the same button, link or menu item: one look lands on the text
    inside a web page's button and the next on the button itself ("Submit Answer" the text, then the button), or on
    its label cut short ("Got It" in the text, "Got It!" the button). The same kind of control, the same app, and the
    same name, or one name the start of the other."""
    first, second = _action(a), _action(b)
    if first is None or second is None or first[0] != second[0] or a.pid != b.pid:
        return False
    x, y = _bare(first[1]), _bare(second[1])
    short, full = sorted((x, y), key=len)
    return x == y or (len(short) >= 3 and full.startswith(short))


def _the_button_meant(now: Target, seen: Target, label: str, found: str = "") -> bool:
    """Whether ``now`` is a button, link or menu item in the same window (and app) as the first look, named like the
    thing meant: what the first look landed on (a web page's text, a group around the button), what that's inside,
    the find_on_screen match aimed at (``found``), or the label the click was asked for, also when the label says
    more around the name ("the “Mean” card"). Web pages answer a look at the same spot with different elements from
    one moment to the next, and a first look UI Automation couldn't describe only knows the window; what ``now``
    does is still checked for risk before the click."""
    action = _action(now)
    if action is None or not now.hwnd or now.hwnd != seen.hwnd or (seen.described and now.pid != seen.pid):
        return False
    name = _bare(action[1])
    if not name:
        return False
    meant = {_bare(n) for n in (seen.name, _action_name(seen), label, found) if n and _bare(n)}
    if any(name == m or (len(m) >= 3 and (name.startswith(m) or m.startswith(name))) for m in meant):
        return True
    whole = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
    return len(name) >= 3 and any(whole.search(_norm(text)) for text in (label, found) if text)


# Controls that keep their place while their name follows what they show: a browser tab's title as its page loads.
_RETITLED = frozenset({"tab"})


def _renamed_in_place(now: Target, seen: Target, label: str = "", found: str = "") -> bool:
    """Whether ``now`` is what the first look saw under a new name: the same kind of control in the same place, window,
    app and dialog, and either a tab (whose title follows its page: "New Tab", then "Decision Briefing" once it has
    loaded) or named as the click meant (its label, or the find_on_screen match aimed at). A pop-up or another kind of
    control there still stops the click, and what ``now`` says still gets its own risk check."""
    if not now.described or not now.hwnd or now.kind != seen.kind or now.container != seen.container:
        return False
    if (now.hwnd, now.pid) != (seen.hwnd, seen.pid) or now.rect is None or seen.rect is None:
        return False
    if not _uia._same_spot(now.rect, seen.rect):
        return False
    if now.kind in _RETITLED:
        return True
    name = _bare(now.name)
    return bool(name) and any(name == _bare(meant) for meant in (label, found) if meant)


# What a look can land on that works nothing itself: a page's or window's surface, a list or tab strip around its
# items, a picture, the window or page itself.
_BLANK = _SURFACES | frozenset({"window", "list", "tab list", "toolbar"})
# What a click picks or switches to besides buttons, links and menu items: a list's option, a row, a tab.
_CHOICES = _ITEM_KINDS | frozenset({"tab"})


def _same_window_and_app(now: Target, seen: Target) -> bool:
    """Whether two looks are in the same top-level window of the same app. A browser's page belongs to one of its
    renderer processes and its window's own frame to the browser process, both the same program (Vivaldi: a page's
    button at one look, the bare pane of the window around it at the next), so the same program counts."""
    if not now.hwnd or now.hwnd != seen.hwnd:
        return False
    if now.pid == seen.pid:
        return True
    return bool(now.process) and now.process.casefold() == seen.process.casefold()


def _works_nothing(t: Target) -> bool:
    """Whether a look landed on nothing that works anything: an unnamed pane, group, picture, text or list, or the
    window or page itself, with no button, link, menu item, option or tab there, not even around it."""
    if not t.described or t.timed_out or t.kind not in _BLANK or (t.name and t.kind not in _TITLED):
        return False
    return _action(t) is None and not any(kind in _CHOICES for kind, _ in t.chain)


def _blank_reread(now: Target, seen: Target) -> bool:
    """Whether the look right before the click landed on nothing that works anything (``_works_nothing``), in the
    same window and app as the risk check's. Browsers answer a look like that while they re-render or hand the spot
    from one of their processes to another (Vivaldi: a tab or a dialog's Close at the risk check, the bare pane of its
    window right before the click), and a dialog closing leaves the page under it. Nothing there can be clicked by
    mistake. What it is still gets its own risk check, and so does what the pointer brings up there."""
    return _works_nothing(now) and _same_window_and_app(now, seen)


def _shallow_first_look(now: Target, seen: Target, label: str = "", found: str = "") -> bool:
    """Whether the risk check's look found only an unnamed group in a web page, or nothing UI Automation could
    describe, and the look right before the click finds a control in the same window and app, with no dialog or menu
    come up around it, and named with every word the click was asked for with (``_has_every_word``). Chromium answers
    the first look at a page with an unnamed group around the spot until it has built that part of its accessibility
    tree (DataCamp and AWS consoles in Vivaldi: an unnamed group at the risk check, then "Submit", "Next" or "Save
    rules" right before the click; 30 refusals on 2026-10-18 alone). The second look knows more than the first, not
    something else, unless it's named otherwise than asked ("emr-5.24.1" for "emr-5.29.0"). A pane, a picture or a
    game's board under the first look is a surface the page really shows, and a button there now still stops the
    click. Only when nobody was asked about the click (the caller's part), and what ``now`` still gets its own risk
    check."""
    if not now.described or now.timed_out or seen.timed_out or now.container != seen.container:
        return False
    if seen.described and not (seen.kind == "group" and seen.web_page and _works_nothing(seen)):
        return False
    if not all(_has_every_word(now, meant) for meant in (label, found) if meant):
        return False
    return _same_window_and_app(now, seen)


_FILLERS = frozenset({"a", "an", "the", "of", "to", "for", "on", "in"})


def _has_every_word(t: Target, meant: str) -> bool:
    """Whether ``t``'s own name (the name of the control it's in, when it has none itself) has every word of
    ``meant``, in order, besides fillers and the words for the kind of thing at its end, and no word that turns it
    around: "Type a message here..." for "message box", "Core Instance(s) size" for "Core size", but not "Virtual
    private cloud (VPC)" for "Core size", "Don't save changes" for "Save changes", or a "Disable" button inside the
    group "Two factor authentication" for "two factor authentication"."""
    action = _action(t)
    name = action[1] if action is not None else t.name or (t.chain[1][1] if len(t.chain) > 1 else "")
    words = _words(name)
    them = [w for w in _core(_words(meant)) if w not in _FILLERS]
    if not them or set(words) & _TURNED - set(them):
        return False
    rest = iter(words)
    return all(w in rest for w in them)  # in order: each word is looked for after the one before


def _words(text: str) -> tuple[str, ...]:
    """The words of a name, without its punctuation: "Refresh: Events" is ("refresh", "events")."""
    return tuple(re.findall(r"\w+", _norm(text)))


# Places to type or pick a value in, which a click is for as much as a button: "the key pair field".
_FIELDS = frozenset({"text box", "drop-down", "spinner"})
# What a label says about the kind of thing at its end, not its name: "Service role dropdown", "default checkbox".
_KIND_WORDS = frozenset(
    {
        "box",
        "button",
        "checkbox",
        "dropdown",
        "entry",
        "field",
        "input",
        "item",
        "link",
        "option",
        "radio",
        "tab",
    }
)
# Words in a name that turn around or change what it does: "Don't save changes" isn't "save changes", nor is "Undo
# send message" or "Close pull request" what "send message" or "the pull request button" meant, unless the label has
# them too. Broader than ``_NEGATED``: here a word anywhere in the name counts, not only right before a risky one.
_TURNED = frozenset(
    {
        "t",
        "dont",
        "not",
        "no",
        "never",
        "cancel",
        "skip",
        "undo",
        "un",
        "stop",
        "block",
        "deny",
        "decline",
        "reject",
        "disable",
        "remove",
        "delete",
        "close",
        "clear",
        "reset",
        "leave",
        "exit",
        "off",
        "unsend",
        "unsubscribe",
    }
)


def _core(words: tuple[str, ...]) -> tuple[str, ...]:
    """A label's words without the words for the kind of thing at its end: ("service", "role") for "Service role
    dropdown"."""
    end = len(words)
    while end > 1 and words[end - 1] in _KIND_WORDS:
        end -= 1
    return words[:end]


def _named_inside(words: tuple[str, ...], them: tuple[str, ...]) -> bool:
    """Whether ``them`` (two words or more) is a run of whole words in a name with no word around it that turns it
    around (``_TURNED``): "key pair" in "Amazon EC2 key pair for SSH to the cluster", not "send message" in "Undo
    send message"."""
    if len(them) < 2 or set(words) & _TURNED - set(them):
        return False
    return any(words[i : i + len(them)] == them for i in range(len(words) - len(them) + 1))


def _the_choice_meant(now: Target, seen: Target, label: str, found: str = "", loose: bool = True) -> bool:
    """Whether ``now`` is the tab, list option, field or button the click was for, in the same window and app as the
    risk check's look: named, word for word whatever the punctuation, as the label, the find_on_screen match aimed at
    (``found``) or what the first look saw, or as a run of whole words in the label or match ("Decision Briefing" for
    "Decision Briefing tab"), or with the label's own words, less what it says about the kind of thing, starting the
    name or a run of two words or more in it ("Service role dropdown" for the button "Service role Choose IAM role",
    "key pair field" for the drop-down "Amazon EC2 key pair for SSH"). A drop-down that opened over the spot between
    the two looks shows the option asked for ("gluelab" in "Existing IAM role", where the first look saw the text
    under it), and a browser that answered the first look at its tab strip with a bare pane shows the tab. Another
    named thing there still stops the click, and what ``now`` is still gets its own risk check. Not ``loose`` (the
    user was asked about the click, and their yes would carry over), the label's words less their kind words don't
    count."""
    name = next(
        (n or now.name for kind, n in now.chain if kind in ACTION_KINDS or kind in _CHOICES or kind in _FIELDS), ""
    )
    words = _words(name)
    if len("".join(words)) < 3 or not _same_window_and_app(now, seen):
        return False
    for meant in (label, found, seen.name, _action_name(seen)):
        them = _words(meant) if meant else ()
        if not them:
            continue
        if them == words or (len("".join(them)) >= 3 and words[: len(them)] == them):
            return True
        if meant not in (label, found):
            continue
        if any(them[i : i + len(words)] == words for i in range(len(them))):
            return True
        if not loose:
            continue
        core = _core(them)
        if core != them and core == words:
            return True
        if _named_inside(words, core):  # also at the start, where one word ("gluelab" of "gluelab-old") isn't enough
            return True
    return False


def _still_there(now: Target, seen: Target, label: str = "", found: str = "") -> bool:
    """Whether a second look at a spot still shows what the risk check saw there, allowing for the ways apps and web
    pages answer one spot differently from one look to the next."""
    return (
        now.same_thing(seen)
        or _same_control(now, seen)
        or _same_surface(now, seen)
        or _the_button_meant(now, seen, label, found)
        or _renamed_in_place(now, seen, label, found)
    )


def _spot_kept(now: Target, seen: Inspection, label: str = "", found: str = "") -> bool:
    """Whether the look right before a click still shows what the risk check saw at the spot (``seen``), allowing for
    the ways apps and web pages answer one spot differently from one look to the next. False stops the click."""
    t = seen.target
    return (
        now.same_thing(t)
        or _same_control(now, t)
        or _same_surface(now, t)
        or _the_button_meant(now, t, label, found)
        or _renamed_in_place(now, t, label, found)
        or _the_choice_meant(now, t, label, found, loose=seen.finding is None)
        or _blank_reread(now, t)
        or (seen.finding is None and _shallow_first_look(now, t, label, found))
    )


def after_click(spot: Target, focus: Target, spot_after: Target, focus_after: Target) -> tuple[tuple[str, ...], str]:
    """What a click did, from a look at its spot and at the keyboard focus right before it and again right after: what
    changed ("window", "focus", "spot", "state"), and a sentence saying so for the model. A check, not proof: a web
    page can change a lot without moving the focus, so nothing changed means look before going on."""
    if any(t.timed_out for t in (spot, focus, spot_after, focus_after)):
        return (), CANT_CHECK
    changed: list[str] = []
    seen: list[str] = []
    if not focus.hwnd or not focus_after.hwnd:
        pass  # her own window was in front (it never counts), or none was: nothing to tell about it
    elif focus_after.hwnd != focus.hwnd:
        changed.append("window")
        app = app_name(focus_after)
        seen.append(f"the active window is now “{_short(focus_after.window, 60)}”" + (f" in {app}" if app else ""))
    elif focus_after.window != focus.window:
        changed.append("window")
        seen.append(f"the window's title is now “{_short(focus_after.window, 60)}”")
    if focus_after.described and (not focus_after.same_thing(focus) or focus_after.rect != focus.rect):
        changed.append("focus")
        seen.append(f"the keyboard focus is on {thing(focus_after)}")
    if not spot_after.same_thing(spot):
        changed.append("spot")
        seen.append(f"that spot shows {thing(spot_after)} now")
    elif spot_after.state != spot.state:
        changed.append("state")
        seen.append(f"it's {spot_after.state} now" if spot_after.state else f"it's no longer {spot.state}")
    if not changed:
        return (), (
            "Nothing I can check changed afterwards (the window, the keyboard focus and that spot are as they were), "
            "which is common on web pages. Look at the screen before doing anything else, and don't click it again "
            "without looking."
        )
    return tuple(changed), "Afterwards " + "; ".join(seen) + "."


def _verb(args: ClickArgs, past: bool = False) -> str:
    base = (
        "double-click"
        if args.double
        else {"left": "click", "right": "right-click", "middle": "middle-click"}[args.button]
    )
    return (base + "ed").capitalize() if past else base


class Click(_Control):
    name = "click"
    description = (
        "Click something on the user's screen. Find it first: find_on_screen for labelled things (exact, "
        "cheapest), else look_at_screen, plus zoom_screen for small or crowded things; then element=N, or x and y "
        "(image='zoom' for the zoom). Mark risky clicks risky=true; the system asks the user itself before them, "
        "so carry a multi-step task through without stopping to ask. The result says what changed afterwards (the "
        "window, the keyboard focus, that spot); when nothing did, or the next step depends on more, look again "
        "first. Prefer a dedicated tool when one fits (open_application, play_on_spotify, "
        "media_control); to drag and drop, use drag. " + TOOK_OVER_RULE
    )
    Args = ClickArgs
    timeout_s = 30.0

    async def inspect_risk(self, args: ClickArgs, ctx: ToolContext) -> RiskFinding | None:
        if current_turn(ctx) is None:
            return None  # it won't run: precheck refuses it
        spot = self._locate(args)
        if isinstance(spot, str):
            return None  # likewise
        target = await self.inspector.point(spot.x, spot.y, avatar_pids(ctx))
        finding = _placed(_site_step(_click_risk(target, args, ctx), target, ctx), target)
        plain = args.button == "left" and not args.double  # a left click in a terminal only puts the focus there
        finding = standing_terminal(finding, target, "" if plain else None)
        args._inspection = Inspection(target, finding, spot)
        return finding

    async def precheck(self, args: ClickArgs, ctx: ToolContext) -> str | None:
        if problem := self._refusal(ctx):
            return problem
        spot = self._locate(args)
        if isinstance(spot, str):
            return spot
        return graded_refusal(args._inspection.target if args._inspection is not None else None, clicking=True)

    async def _what_it_did(
        self, spot: Spot, pids: frozenset[int], clicked: Target, focus: Target
    ) -> tuple[tuple[str, ...], str]:
        await self.sleep(AFTER_S)
        spot_after = await self._look(self.inspector.point(spot.x, spot.y, pids), AFTER_BUDGET_S)
        return after_click(clicked, focus, spot_after, await self._look(self.inspector.focus(pids), AFTER_BUDGET_S))

    async def _click_by(self, way: str, spot: Spot, args: ClickArgs) -> None:
        """Click at ``spot``: "plain" as always, or "slow" for a streamed surface that missed a quick click (the
        pointer moved off and back onto the spot so the far end sees it arrive, a rest, the button held longer)."""
        if way == "plain":
            await asyncio.to_thread(self.desktop.click, spot.x, spot.y, args.button, args.double)
            return
        await asyncio.to_thread(self.desktop.move, spot.x + 3, spot.y + 3)
        await self.sleep(SLOW_STEP_S)
        await asyncio.to_thread(self.desktop.move, spot.x, spot.y)
        await self.sleep(SLOW_HOVER_S)
        await asyncio.to_thread(self.desktop.button, args.button, True)
        try:
            await self.sleep(SLOW_HOLD_S)
        finally:
            self.desktop.button(args.button, False)  # at once, even when cancelled: a button left down drags

    async def describe_call(self, args: ClickArgs, ctx: ToolContext) -> str:
        target = args._inspection.target if args._inspection is not None else None
        what = _name_of(target) if target is not None else ""
        app = app_name(target) if target is not None else ""
        return f"{_verb(args)} {what or args.label or 'that spot'}" + (f" in {app}" if app else "")

    async def _act(self, args: ClickArgs, ctx: ToolContext) -> ToolResult:
        turn, problem = self._start(ctx)
        if turn is None:
            return ToolResult.error(problem or NOT_IN_TURN)
        seen = args._inspection
        spot = seen.spot if seen is not None and seen.spot is not None else self._locate(args)
        if isinstance(spot, str):
            return ToolResult.error(spot)
        if problem := self._stop(ctx, turn):
            return ToolResult.error(problem)
        pids = avatar_pids(ctx)
        # Point first: the user sees what she's about to click, and she flies clear of it.
        seen_target = seen.target if seen is not None else None
        label = args.label or (_name_of(seen_target) if seen_target is not None else "")
        pointed = self._point(ctx, spot, spot.box or _pointing_box(seen_target), label)
        if pointed:
            await self.sleep(POINT_WAIT_S)
            if problem := self._stop(ctx, turn):
                return ToolResult.error(problem)
        # To tell what the click did; now rather than right before it, so nothing slow comes between the last look at
        # the spot and the click (the pointer moving there doesn't move the focus).
        focus = await self._look(self.inspector.focus(pids))
        # Still what the risk check saw? The user may have taken a while to answer, or the screen moved on.
        now = await self.inspector.point(spot.x, spot.y, pids)
        if problem := graded_refusal(now, clicking=True):
            return ToolResult.error(problem)
        if seen is None:  # the risk check didn't run, so nobody was asked
            if (found := _click_risk(now, args, ctx)) is not None and not _free_step(now, ctx, found):
                return ToolResult.error(_needs_yes(found))
            seen = Inspection(now, None, spot)
        elif (
            not (same := now.same_thing(seen.target) or _same_control(now, seen.target))
            and not _spot_kept(now, seen, args.label, spot.name)
            and not _free_change(now, seen, args, ctx)
        ):
            log.info(
                "Click refused, the spot changed: checked %s %r (in %s, window %s, pid %s), now %s %r (in %s, window "
                "%s, pid %s), label %r, containers %r %r, asked %s",
                seen.target.kind,
                seen.target.name,
                seen.target.chain,
                seen.target.hwnd,
                seen.target.pid,
                now.kind,
                now.name,
                now.chain,
                now.hwnd,
                now.pid,
                args.label,
                seen.target.container,
                now.container,
                seen.finding is not None,
            )
            return ToolResult.error(
                f"The screen changed at that spot since I checked it: it's {thing(now)} now, so I didn't click. "
                "Look at the screen again first."
            )
        elif (not same or now.name != seen.target.name) and (found := _click_risk(now, args, ctx)) is not None:
            # The same link with its label cut short differently, a web page's surface under another role, the option
            # meant in a drop-down that just opened, a browser's bare pane: what it says now may say more than the
            # check saw.
            if (seen.finding is None or found.scope != seen.finding.scope) and not _free_step(now, ctx, found):
                return ToolResult.error(
                    f"That spot now shows {thing(now)}, which {found.reason}, so I didn't click. "
                    "Look at the screen again first."
                )
        if problem := self._stop(ctx, turn):
            return ToolResult.error(problem)
        try:
            where = await asyncio.to_thread(self.desktop.move, spot.x, spot.y)
        except OSError as exc:
            return ToolResult.error(f"I couldn't move the pointer there ({exc}).")
        await self.sleep(HOVER_S)
        self.watch.acted(turn.id, where)
        # With the pointer on it: hovering can bring up something else in that spot, like a row's delete icon.
        if await asyncio.to_thread(self._avatar_under, spot.x, spot.y, pids):
            if await _pins_aside(ctx):
                return ToolResult.error(
                    "Something of mine was in the way of that spot, so I didn't click. My pinned panels are out of the "
                    "way for now: click again. If it's refused again, it's the avatar or a panel that isn't pinned."
                )
            return ToolResult.error(
                "The avatar or one of my pop-up panels is in the way of that spot, so I didn't click. Close the panel "
                "(close_visual) or try again."
            )
        hovered = await self.inspector.point(spot.x, spot.y, pids)
        if (
            not hovered.same_thing(now)
            and not _same_control(hovered, now)
            and not _the_button_meant(hovered, now, label, spot.name)
        ):
            found = _click_risk(hovered, args, ctx)
            if found is not None and (seen.finding is None or found.scope != seen.finding.scope):
                return ToolResult.error(
                    f"With the pointer on it, that spot shows {thing(hovered)}, which {found.reason}, so I didn't "
                    "click. Look at the screen again first."
                )
        clicked = hovered if hovered.described else now
        # Inside a drawn surface UI Automation can't tell what the click did: the screen there is copied first, and a
        # quick click that shows nothing is followed by a slow one, unless it's double or risky (twice could be worse).
        surface = drawn_surface(clicked)
        window = (clicked.hwnd, clicked.pid)
        ways = ("plain",)
        if surface and not args.double and seen.finding is None:
            ways = self.delivery.order(window, "click", ("plain", "slow"))
        rect = _landing.region(spot.x, spot.y, clicked.rect) if surface else None
        frames = await self.landing.before(rect) if rect is not None else None
        await self._copy_before("your click")
        if problem := self._stop(ctx, turn):
            return ToolResult.error(problem)
        if pointed:
            self._press(ctx)  # she jabs at it as it's clicked
        try:
            await self._click_by(ways[0], spot, args)
        except OSError as exc:
            return ToolResult.error(f"The click didn't go through ({exc}).")
        await self.sleep(SETTLE_S)
        self.watch.acted(turn.id, where)
        if args.button == "left":
            _last_clicks[self.watch] = (turn.id, clicked, (spot.x, spot.y))
        else:
            _last_clicks.pop(self.watch, None)  # a menu opened instead
        app = app_name(clicked)
        content = f"{_verb(args, past=True)} {thing(clicked)}" + (f" in {app}." if app else ".")
        try:
            changed, says = await asyncio.wait_for(self._what_it_did(spot, pids, clicked, focus), AFTER_BUDGET_S)
        except Exception:
            log.warning("Telling what a click did failed", exc_info=True)
            changed, says = (), CANT_CHECK
        if surface:
            landed = await self.landing.after(frames, typing=False)
            tried = [ways[0]]
            for way in ways[1:]:
                # Only when nothing at all showed it: a window or focus that moved, or the spot showing something
                # else now (a page loading), means the next click would land somewhere new.
                if landed is not False or changed:
                    break
                if problem := self._stop(ctx, turn):
                    return ToolResult.error(f"{content} Nothing on the screen there changed. {problem}")
                again = await self._look(self.inspector.point(spot.x, spot.y, pids))
                if again.timed_out or not again.same_thing(clicked):
                    break
                log.info("A click into a drawn surface in %s showed nothing: trying a %s one", app, way)
                frames = await self.landing.before(rect) if rect is not None else None
                try:
                    await self._click_by(way, spot, args)
                except OSError as exc:
                    return ToolResult.error(
                        f"{content} Nothing on the screen there changed, and a slower click didn't go through ({exc})."
                    )
                await self.sleep(SETTLE_S)
                self.watch.acted(turn.id, where)
                tried.append(way)
                landed = await self.landing.after(frames, typing=False)
            if landed is False and not changed:
                _last_clicks.pop(self.watch, None)  # typing mustn't follow it in
                log.info(
                    "A click into a drawn surface in %s showed nothing on screen (tried %s)", app, ", ".join(tried)
                )
                again = ", then again more slowly," if len(tried) > 1 else ""
                return ToolResult.error(
                    f"{content[:-1]}{again} but nothing on the screen there changed. It's drawn rather than described "
                    "(a streamed or embedded screen, a canvas), so UI Automation can't see in either: the click most "
                    "likely didn't reach what's inside. Look at the screen before going on, and don't say it worked.",
                    data={"x": spot.x, "y": spot.y, "app": app, "changed": [], "landed": False},
                )
            if landed:
                self.delivery.worked(window, "click", tried[-1])
                slower = " A quick click showed nothing; a slower one did." if len(tried) > 1 else ""
                if changed:
                    says += " The screen there changed too." + slower
                else:
                    says = (
                        "UI Automation can't see inside it (it's drawn: a streamed or embedded screen, a canvas), but "
                        "the screen there changed, so it landed. Look to see what it did." + slower
                    )
                changed = (*changed, "screen")
            elif landed is False:
                says += " Nothing on the screen there changed, though, so look before going on."
            elif not changed:
                says += " I couldn't check the screen there either."
        return ToolResult.success(
            f"{content} {says}",
            data={
                "x": spot.x,
                "y": spot.y,
                "button": args.button,
                "double": args.double,
                "app": app,
                "changed": list(changed),
                "name": clicked.name,  # a Send button's click is a send (``claims``)
            },
        )


# ------------------------------------------------------------------ type_text


class TypeTextArgs(ToolArgs):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)  # spaces and line breaks are the text

    text: str = Field(
        min_length=1, max_length=2000, description="Exactly as it should appear; line breaks are new lines."
    )
    enter: bool = Field(default=False, description="Press Enter afterwards (usually sends or submits).")
    _inspection: Inspection | None = PrivateAttr(default=None)

    @field_validator("text")
    @classmethod
    def _only_text(cls, value: str) -> str:
        return clean_text(value)


# Where a code editor or a search box moves the keyboard focus while you type: its suggestion list (and the
# suggestion picked in it). Typing goes on into the editor; a button or a link taking the focus still stops it.
_SUGGESTIONS = frozenset({"list", "list item", "tool tip", "menu", "menu item", "combo box"})


def _still_typing_here(focus: Target, typing_into: Target, args: TypeTextArgs) -> bool:
    """Whether the focus that moved mid-typing is still where this text belongs: in the same window and app, the same
    text box (the editor took the focus back, under another element) or its suggestion list, and nothing there that
    the risk check would now ask about (a password box, a console)."""
    if (focus.hwnd, focus.pid) != (typing_into.hwnd, typing_into.pid) or focus.password:
        return False
    if not (focus.kind in _SUGGESTIONS or (focus.editable and focus.same_thing(typing_into))):
        return False  # a button, a link, or another text field (typing there would put the text in the wrong place)
    found = classify_typing(focus, args.text, args.enter)
    return found is None or found == classify_typing(typing_into, args.text, args.enter)


# How each way of typing into a drawn surface is said.
_WAYS_SAID = {"keys": "as key presses", "paste": "by pasting it"}


def _no_key(ch: str) -> None:
    """For a desktop that can't say where keys are: every character goes as Unicode."""
    return None


class TypeText(_Control):
    name = "type_text"
    description = (
        "Type text into whatever has the keyboard focus (any language, emoji, line breaks). Click the text field "
        "first so it goes where you mean. Never type passwords, codes or other secrets unless the user dictated "
        "them in this conversation. To show the user a note or list, use show_note, not Notepad (it reopens their "
        "own files). The system asks the user itself before risky typing, so don't ask first. " + TOOK_OVER_RULE
    )
    Args = TypeTextArgs
    timeout_s = 60.0
    private_args = frozenset({"text"})  # what she types may be a password: logs and events get its length only

    async def inspect_risk(self, args: TypeTextArgs, ctx: ToolContext) -> RiskFinding | None:
        if current_turn(ctx) is None:
            return None
        target = await self.inspector.focus(avatar_pids(ctx))
        finding = _placed(_trust(classify_typing(target, args.text, args.enter), target, ctx), target)
        finding = standing_terminal(finding, target, args.text)
        args._inspection = Inspection(target, finding)
        return finding

    async def precheck(self, args: TypeTextArgs, ctx: ToolContext) -> str | None:
        if problem := self._refusal(ctx):
            return problem
        if args._inspection is None:
            return None
        target = args._inspection.target
        return graded_refusal(target) or typing_problem(target, self._clicked(current_turn(ctx)))

    async def describe_call(self, args: TypeTextArgs, ctx: ToolContext) -> str:
        target = args._inspection.target if args._inspection is not None else None
        app = app_name(target) if target is not None else ""
        if target is not None and target.password:  # said aloud: never read out what goes into a password box
            return f"type {len(args.text)} characters into the password box" + (f" in {app}" if app else "")
        lines = args.text.count("\n") + 1
        text = f"type “{_preview(args.text)}”" + (f" ({lines} lines)" if lines > 1 else "")
        return text + (f" into {app}" if app else "") + (" and press Enter" if args.enter else "")

    async def _act(self, args: TypeTextArgs, ctx: ToolContext) -> ToolResult:
        turn, problem = self._start(ctx)
        if turn is None:
            return ToolResult.error(problem or NOT_IN_TURN)
        if problem := self._stop(ctx, turn):
            return ToolResult.error(problem)
        seen = args._inspection
        now = await self._focus_back(await self.inspector.focus(avatar_pids(ctx)), seen, ctx)
        if seen is None:
            if (found := _trust(classify_typing(now, args.text, args.enter), now, ctx)) is not None:
                return ToolResult.error(_needs_yes(found))
        elif now.hwnd != seen.target.hwnd or not now.same_thing(seen.target):
            where = app_name(now) or "another window"
            return ToolResult.error(
                f"The keyboard focus moved since I checked it (it's in {where} now), so I didn't type. Click the text "
                "field again first."
            )
        if problem := typing_problem(now, self._clicked(turn)):
            return ToolResult.error(problem)
        clicked, at = self._clicked(turn), self._clicked_at(turn)
        if clicked is not None and at is not None and clicked_into_surface(now, clicked):
            return await self._type_into_surface(args, ctx, turn, now, at)
        if not args.enter and "\n" in args.text and is_shell(now):
            return await self._paste_lines(args, ctx, turn, now)
        # Where Enter sends, a line break inside the text is Shift+Enter: a new line, not a message per line.
        newline = (VK_LSHIFT, VK_RETURN) if enter_sends(now) else (VK_RETURN,)
        total, typed = len(args.text), 0
        pids = avatar_pids(ctx)
        focused = await self.inspector.focus_id(pids)

        def stopped(problem: str) -> ToolResult:
            done = f"I typed {typed} of {total} characters, then stopped. " if typed else ""
            return ToolResult.error(done + problem, data={"typed": typed})

        async def moved_on() -> str | None:
            """Why to stop before the next chunk: the turn, the user, or the focus gone somewhere else, even within
            the window (a page moving it to a button, where a typed space would press it)."""
            if problem := self._stop(ctx, turn) or self._focus_moved(now):
                return problem
            nonlocal focused
            if typed and focused is not None and (current := await self.inspector.focus_id(pids)) is not None:
                if current != focused:
                    if not _still_typing_here(await self.inspector.focus(pids), now, args):
                        return "The keyboard focus moved to something else while I was typing, so I stopped."
                    focused = current  # a code editor's suggestion list, or its own text box again: carry on
            return None

        if not args.enter:
            await self._copy_before("your typing")
        for chunk in chunks(args.text, CHUNK_CHARS):
            if problem := await moved_on():
                return stopped(problem)
            try:
                sent = await asyncio.to_thread(self.desktop.keys, text_strokes(chunk, newline))
            except OSError as exc:
                return stopped(f"Windows didn't take the keys ({exc}).")
            typed += len(chunk)
            await self.sleep(SETTLE_S)
            self.watch.acted(turn.id, sent=sent)
        outcome = ""
        if args.enter:
            await self._copy_before("pressing Enter")
            if problem := await moved_on():
                return stopped(problem)
            checking = checks_send(now)
            before = await self._before_enter(pids) if checking else (None, None)
            try:
                sent = await asyncio.to_thread(self.desktop.keys, combo_strokes((VK_RETURN,)))
            except OSError as exc:
                return stopped(f"Windows didn't take Enter ({exc}).")
            await self.sleep(SETTLE_S)
            self.watch.acted(turn.id, sent=sent)
            if checking:  # did it go? Her "I sent it" is held to this (``claims``)
                outcome = await self._sent(args.text, before, now, pids)
        app = app_name(now)
        content = (
            f"Typed {total} characters"
            + (f" into {app}" if app else "")
            + (" and pressed Enter." if args.enter else ".")
            + (f" {SAID_SENT[outcome]}" if outcome else "")
        )
        data: dict[str, Any] = {"typed": total, "enter": args.enter, "app": app}
        if outcome:
            data["sent"] = outcome
        return ToolResult.success(content, data=data)

    async def _paste_lines(self, args: TypeTextArgs, ctx: ToolContext, turn: TurnInfo, now: Target) -> ToolResult:
        """Text with line breaks into a terminal, not to be sent: as a paste. Typed, each line break is an Enter that
        sends a prompt or runs a command (2026-11-01: a list typed into Claude Code's prompt went off unfinished);
        pasted, the terminal hands it over as one paste and the app keeps the line breaks as new lines."""
        if problem := self._stop(ctx, turn) or self._focus_moved(now):
            return ToolResult.error(problem)
        try:
            held = await asyncio.to_thread(self.clip.held_text)
        except Exception:
            log.debug("Reading the clipboard before a paste failed", exc_info=True)
            held = (False, None)
        if not held[0]:
            return ToolResult.error(
                "The text has line breaks, which a terminal takes as Enter, and I couldn't paste it instead (the "
                "clipboard holds something I couldn't put back), so I typed nothing. Type it as one line, or tell the "
                "user.",
                data={"typed": 0},
            )
        await self._copy_before("your typing")
        try:
            await asyncio.to_thread(self.clip.write_clipboard, args.text, True)
            try:
                sent = await asyncio.to_thread(self.desktop.keys, paste_strokes(_no_key))
                await self.sleep(PASTE_HOLD_S)
            finally:
                await asyncio.to_thread(self.clip.put_back, held[1])
        except OSError as exc:
            return ToolResult.error(f"Windows didn't take the paste ({exc}).")
        except Exception as exc:
            return ToolResult.error(f"The clipboard couldn't be used for a paste ({exc}).")
        self.watch.acted(turn.id, sent=sent)
        app, total = app_name(now), len(args.text)
        content = (
            f"Typed {total} characters" + (f" into {app}" if app else "") + " by pasting them, so the line breaks "
            "didn't press Enter. Nothing was sent."
        )
        return ToolResult.success(content, data={"typed": total, "enter": False, "app": app, "way": "paste"})

    async def _type_into_surface(
        self, args: TypeTextArgs, ctx: ToolContext, turn: TurnInfo, now: Target, at: tuple[int, int]
    ) -> ToolResult:
        """Type into the drawn surface her last click landed in (a streamed remote screen), where UI Automation sees
        only the page: as key presses by scan code ("keys"), then as a paste ("paste"), whichever worked in this
        window last going first, each checked on the screen around the click; failed out loud when neither shows."""
        window, app, total = (now.hwnd, now.pid), app_name(now), len(args.text)
        key_for = getattr(self.desktop, "key_for", None)
        newline = (SCAN_LSHIFT, SCAN_RETURN) if enter_sends(now) else (SCAN_RETURN,)
        rect = _landing.region(*at)
        tried: list[str] = []
        landed: bool | None = False
        how = ""
        for way in self.delivery.order(window, "type", ("keys", "paste")):
            if problem := self._stop(ctx, turn) or self._focus_moved(now):
                return ToolResult.error(problem)
            held: tuple[bool, str | None] = (True, None)
            if way == "paste":
                try:
                    held = await asyncio.to_thread(self.clip.held_text)
                except Exception:
                    log.debug("Reading the clipboard before a paste failed", exc_info=True)
                    held = (False, None)
                if not held[0]:
                    tried.append("paste (skipped: the clipboard holds something I couldn't put back)")
                    continue
            frames = await self.landing.before(rect)
            if not tried:
                await self._copy_before("your typing")
            if way == "keys":
                for chunk in chunks(args.text, CHUNK_CHARS):
                    if problem := self._stop(ctx, turn) or self._focus_moved(now):
                        return ToolResult.error(problem)
                    strokes = scan_strokes(chunk, key_for, newline) if key_for else text_strokes(chunk)
                    try:
                        sent = await asyncio.to_thread(self.desktop.keys, strokes)
                    except OSError as exc:
                        return ToolResult.error(f"Windows didn't take the keys ({exc}).")
                    await self.sleep(SETTLE_S)
                    self.watch.acted(turn.id, sent=sent)
            else:
                try:
                    await asyncio.to_thread(self.clip.write_clipboard, args.text, True)
                    try:
                        sent = await asyncio.to_thread(self.desktop.keys, paste_strokes(key_for or _no_key))
                        await self.sleep(PASTE_HOLD_S)
                    finally:
                        await asyncio.to_thread(self.clip.put_back, held[1])
                except OSError as exc:
                    return ToolResult.error(f"Windows didn't take the paste ({exc}).")
                except Exception as exc:
                    return ToolResult.error(f"The clipboard couldn't be used for a paste ({exc}).")
                self.watch.acted(turn.id, sent=sent)
            landed = await self.landing.after(frames, typing=True)
            if landed is not False:
                how = way
                break
            tried.append(way)
            log.info("Typing into a drawn surface in %s showed nothing on screen as %s", app, way)
        if landed is False:
            ways = " and ".join(_WAYS_SAID.get(w, w) for w in tried)
            return ToolResult.error(
                f"I typed the text into {app or 'the window'} {ways}, but nothing on the screen where I clicked changed: "
                "it most likely didn't reach what's inside (a streamed or embedded screen UI Automation can't see "
                "into). Look at the screen, click right into the text field there, and try again; don't say it was "
                "typed.",
                data={"typed": 0, "app": app, "landed": False},
            )
        if landed:
            self.delivery.worked(window, "type", how)
        if args.enter:
            if problem := self._stop(ctx, turn) or self._focus_moved(now):
                return ToolResult.error(f"I typed the text, then stopped before Enter. {problem}")
            await self._copy_before("pressing Enter")
            enter = scan_strokes("\n", key_for) if key_for else combo_strokes((VK_RETURN,))
            try:
                sent = await asyncio.to_thread(self.desktop.keys, enter)
            except OSError as exc:
                return ToolResult.error(f"I typed the text, but Windows didn't take Enter ({exc}).")
            await self.sleep(SETTLE_S)
            self.watch.acted(turn.id, sent=sent)
        said = _WAYS_SAID[how] if how else "as key presses"
        shown = (
            "It showed on the screen there."
            if landed
            else "I couldn't check the screen there, so look before going on."
        )
        pasted = (
            " A streamed screen pastes from its own clipboard unless it shares this PC's, so look that it's the right "
            "text."
            if how == "paste"
            else ""
        )
        content = (
            f"Typed {total} characters into the streamed or embedded screen in {app or 'the window'} {said}"
            + (" and pressed Enter. " if args.enter else ". ")
            + shown
            + pasted
        )
        return ToolResult.success(content, data={"typed": total, "enter": args.enter, "app": app, "way": how or "keys"})


# ------------------------------------------------------------------ press_keys


class PressKeysArgs(ToolArgs):
    keys: str = Field(
        min_length=1,
        max_length=60,
        description="A key or shortcut: 'enter', 'escape', 'tab', 'page_down', 'f5', 'ctrl+c', 'alt+tab', 'win+d'.",
    )
    times: int = Field(default=1, ge=1, le=20, description="How many times to press it.")
    _inspection: Inspection | None = PrivateAttr(default=None)

    @field_validator("keys")
    @classmethod
    def _known_keys(cls, value: str) -> str:
        return parse_keys(value).text  # the canonical spelling: "Ctrl + Shift + T" is "ctrl+shift+t"


class PressKeys(_Control):
    name = "press_keys"
    description = (
        "Press a key or shortcut in the active window (modifiers ctrl, alt, shift, win joined to one key with +). "
        "For text, use type_text. The system asks the user itself before risky keys, so don't ask first. "
        + TOOK_OVER_RULE
    )
    Args = PressKeysArgs
    timeout_s = 30.0

    async def inspect_risk(self, args: PressKeysArgs, ctx: ToolContext) -> RiskFinding | None:
        if current_turn(ctx) is None:
            return None
        target = await self.inspector.focus(avatar_pids(ctx))
        combo = parse_keys(args.keys)
        finding = _trust(classify_keys(target, combo), target, ctx)
        if combo.text in ("enter", "space"):
            finding = _site_step(finding, target, ctx)  # presses the focused button
        elif (
            combo.text == "delete"
            and finding is not None
            and finding.scope == "delete"
            and _editing_answer(target, ctx)
        ):
            finding = None  # clearing the code in the exercise's editor (after Ctrl+A) asked every time
        finding = _placed(finding, target)
        finding = standing_terminal(finding, target, await self._adds_to_line(combo, finding, target))
        args._inspection = Inspection(target, finding)
        return finding

    async def _adds_to_line(self, combo: KeyCombo, finding: RiskFinding | None, t: Target) -> str | None:
        """What pressing ``combo`` in a terminal adds to its command line, for ``standing_terminal``: "" for Enter or
        Ctrl+C (they run or stop what's there), the clipboard's text for a paste, None for anything else or a
        clipboard that holds no text (asked about each time)."""
        if finding is None or not is_terminal(t):
            return None
        if combo.text in _TERMINAL_KEYS:
            return ""
        if combo.text not in PASTE_KEYS:
            return None
        try:
            ok, text = await asyncio.to_thread(self.clip.held_text)
        except Exception:
            log.debug("Reading the clipboard to judge a paste failed", exc_info=True)
            return None
        return text if ok and text else None

    async def precheck(self, args: PressKeysArgs, ctx: ToolContext) -> str | None:
        target = args._inspection.target if args._inspection is not None else None
        combo = parse_keys(args.keys)
        return self._refusal(ctx) or _own_hotkey(ctx, combo) or graded_refusal(target, clicking=_presses(combo))

    async def describe_call(self, args: PressKeysArgs, ctx: ToolContext) -> str:
        target = args._inspection.target if args._inspection is not None else None
        app = app_name(target) if target is not None else ""
        times = f" {args.times} times" if args.times > 1 else ""
        return f"press {parse_keys(args.keys).label}{times}" + (f" in {app}" if app else "")

    async def _act(self, args: PressKeysArgs, ctx: ToolContext) -> ToolResult:
        turn, problem = self._start(ctx)
        if turn is None:
            return ToolResult.error(problem or NOT_IN_TURN)
        combo = parse_keys(args.keys)
        if problem := _own_hotkey(ctx, combo) or self._stop(ctx, turn):
            return ToolResult.error(problem)
        seen = args._inspection
        now = await self._focus_back(await self.inspector.focus(avatar_pids(ctx)), seen, ctx)
        if problem := graded_refusal(now, clicking=_presses(combo)):
            return ToolResult.error(problem)
        if seen is None:
            if (found := _trust(classify_keys(now, combo), now, ctx)) is not None:
                return ToolResult.error(_needs_yes(found))
        elif now.hwnd != seen.target.hwnd or not now.same_thing(seen.target):
            where = app_name(now) or "another window"
            return ToolResult.error(
                f"The keyboard focus moved since I checked it (it's in {where} now), so I didn't press anything. "
                "Look again first."
            )
        await self._copy_before(f"pressing {combo.label}")
        pids = avatar_pids(ctx)
        checking = combo.text in SENDING_KEYS and args.times == 1 and checks_send(now)
        before = await self._before_enter(pids) if checking else (None, None)
        for press in range(args.times):
            # The active window changing after a press (Alt+Tab) ends the run: the rest would go somewhere unchecked.
            if problem := self._stop(ctx, turn) or self._focus_moved(now):
                done = f"I pressed {combo.label} {press} times, then stopped. " if press else ""
                return ToolResult.error(done + problem)
            try:
                sent = await asyncio.to_thread(self.desktop.keys, combo_strokes(combo.vks))
            except OSError as exc:
                return ToolResult.error(f"Windows didn't take the keys ({exc}).")
            await self.sleep(KEY_GAP_S)
            self.watch.acted(turn.id, sent=sent)
        outcome = await self._sent("", before, now, pids) if checking else ""
        app = app_name(now)
        times = f" {args.times} times" if args.times > 1 else ""
        content = f"Pressed {combo.label}{times}" + (f" in {app}." if app else ".")
        data: dict[str, Any] = {"keys": combo.text, "times": args.times, "app": app}
        if outcome:
            content += f" {SAID_SENT[outcome]}"
            data["sent"] = outcome
        return ToolResult.success(content, data=data)


# ------------------------------------------------------------------ scroll


class ScrollArgs(_SpotArgs):
    direction: Literal["up", "down", "left", "right"]
    amount: int = Field(default=3, ge=1, le=30, description="Wheel notches, ~3 lines each.")

    @model_validator(mode="after")
    def _maybe_a_target(self) -> Self:
        self._check_spot(required=False)
        return self


@dataclass
class _Scrolling:
    """Her scrolling in one turn (the conversation's, or a screen task's: ``source`` is its turn getter), on the
    screen memory's clock: since when, for the user starting to talk, and when last, for a look in between."""

    source: object
    turn: int
    since: float
    last: float


class Scroll(_Control):
    name = "scroll"
    description = (
        "Scroll with the mouse wheel. To scroll a particular area, aim like click (element, or x and y); without "
        "them it scrolls wherever the pointer is. Look again after every scroll: the next is refused until you "
        f"have. To read something long (a chat history, an article), go {READ_NOTCHES} notches at a time, so each "
        "view overlaps the last and no line is skipped. If the user starts talking, scrolling stops for the turn: "
        "let them speak. " + TOOK_OVER_RULE
    )
    Args = ScrollArgs
    timeout_s = 20.0
    _scrolling: _Scrolling | None = None

    async def precheck(self, args: ScrollArgs, ctx: ToolContext) -> str | None:
        if problem := self._refusal(ctx):
            return problem
        turn = current_turn(ctx)
        if turn is not None and (problem := self._not_again(ctx, turn)):
            return problem
        spot = self._locate(args) if args.has_spot else None
        return spot if isinstance(spot, str) else None

    def _this_turn(self, ctx: ToolContext, turn: TurnInfo) -> _Scrolling | None:
        s = self._scrolling
        return s if s is not None and s.source is ctx.extras.get("turn") and s.turn == turn.id else None

    def _talked(self, ctx: ToolContext, since: float) -> bool:
        heard = heard_talking(ctx)
        return heard is not None and heard >= since

    def _not_again(self, ctx: ToolContext, turn: TurnInfo) -> str | None:
        """Why she mustn't scroll again in this turn: the user started talking since her first scroll (they
        shouldn't have to grab the mouse to be heard), or she hasn't looked since her last (a run of scrolls
        unseen skips what's between them)."""
        s = self._this_turn(ctx, turn)
        if s is None:
            return None
        if self._talked(ctx, s.since):
            return USER_TALKING
        if self.memory.looked_at() < s.last:
            return LOOK_FIRST
        return None

    async def describe_call(self, args: ScrollArgs, ctx: ToolContext) -> str:
        return f"scroll {args.direction}"

    async def _act(self, args: ScrollArgs, ctx: ToolContext) -> ToolResult:
        turn, problem = self._start(ctx)
        if turn is None:
            return ToolResult.error(problem or NOT_IN_TURN)
        if problem := self._stop(ctx, turn) or self._not_again(ctx, turn):
            return ToolResult.error(problem)
        if args.has_spot:
            spot = self._locate(args)
            if isinstance(spot, str):
                return ToolResult.error(spot)
            try:
                where = await asyncio.to_thread(self.desktop.move, spot.x, spot.y)
            except OSError as exc:
                return ToolResult.error(f"I couldn't move the pointer there ({exc}).")
            await self.sleep(HOVER_S)
            self.watch.acted(turn.id, where)
        else:
            where = await asyncio.to_thread(self.desktop.cursor)
        # The wheel goes to whatever is under the pointer, and that mustn't be her.
        if await asyncio.to_thread(self._avatar_under, where[0], where[1], avatar_pids(ctx)):
            if await _pins_aside(ctx):
                return ToolResult.error("Something of mine was under the pointer there. My pinned panels are out of the way for now: scroll again.")  # fmt: skip
            return ToolResult.error(
                "The avatar or one of my pop-up panels is under the pointer there. Give a spot to scroll (element, or "
                "x and y), or close the panel (close_visual)."
            )
        if self.before is not None:
            self.before.forget()  # what scrolling shows isn't the change her last click or key press made
        s = self._this_turn(ctx, turn)
        since = s.since if s is not None else self.memory.now()
        done = 0
        try:
            while done < args.amount:
                problem = self._stop(ctx, turn) or (USER_TALKING if self._talked(ctx, since) else None)
                if problem:
                    prefix = f"I scrolled {done} notches, then stopped. " if done else ""
                    return ToolResult.error(prefix + problem)
                step = min(SCROLL_STEP, args.amount - done)
                try:
                    await asyncio.to_thread(self.desktop.wheel, args.direction, step)
                except OSError as exc:
                    return ToolResult.error(f"Windows didn't take the scrolling ({exc}).")
                done += step
                await self.sleep(SETTLE_S)
                self.watch.acted(turn.id)
        finally:
            if done:
                source = ctx.extras.get("turn")
                self._scrolling = _Scrolling(source, turn.id, since, self.memory.now())
        skipped = (
            " That's more than a screenful in most apps, so what was between went by unseen: when reading, go "
            f"{READ_NOTCHES} at a time."
            if args.amount > PAGE_NOTCHES
            else ""
        )
        return ToolResult.success(
            f"Scrolled {args.direction} {args.amount} notches.{skipped} Look again to see what's there now.",
            data={"direction": args.direction, "amount": args.amount},
        )


TOOLS = [Click, TypeText, PressKeys, Scroll]
