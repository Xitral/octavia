"""What learning by watching may learn and offer, and how much evidence it takes. Protected from self-improvement
builds (evolve/policy.py FORBIDDEN): a build can't lower the bar, widen what's watched or let an offer act.

* Only names of apps and of her own read-only tools are ever learned: never a window title, a page, a file or the
  words of a request. Windows' own shell and helper processes, installers, password managers and the apps the user
  keeps out of screen history (``recall.privacy.excluded_app`` plus their list) are never learned.
* What she may offer to ask for again is a short list of read-only tools (``ASK_TOOLS``): a "yes" can only ever make
  her look something up. Apps are offered to be opened (``open_application``, which only starts an app).
* A routine is learned from at least ``min_days`` separate days and on at least ``min_support`` of the days it could
  have happened, out of at least ``MIN_HISTORY_DAYS`` days of records: three days in a row is a coincidence, not a
  habit. Below that it's only "watching" (listed, never offered).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

DAY_START_H = 4  # a day runs 04:00 to 04:00: the 1 AM end of an evening belongs to that evening
MIN_HISTORY_DAYS = 7  # days between the first and the last record before anything can be learned
WINDOW_MIN = 60  # a time-of-day routine happens within the same hour
OFFER_BEFORE_MIN = 10  # offered from this long before its usual time...
OFFER_AFTER_MIN = 30  # ...until this long after it
SESSION_GAP_S = 45 * 60  # this long with no app coming to the front: a new sitting at the PC
FOLLOW_S = 10 * 60  # "after A, B": B comes up within this of A, in the same sitting
TRIGGER_S = 3 * 60  # an "after A" offer is only timely this soon after A came up
AFTER_LIFT = 0.2  # B must follow A this much more often than B turns up in a sitting at all
ACTIVE_S = 120  # offered only while the user is at the PC (input within this)
OUTCOME_S = 15 * 60  # did they open it (or ask for it) within this of the offer: taken up
MAX_MISSES = 3  # offers in a row not taken up: she stops offering that one (it's "retired")
WATCH_MIN_DAYS = 3  # shown as "watching" (never offered) from this many days...
WATCH_MIN_SUPPORT = 0.3  # ...and this share of the days

# Windows itself and helpers that come to the front without anyone choosing them; installers and updaters.
SYSTEM_PROCESSES: frozenset[str] = frozenset(
    {
        "explorer.exe",
        "dwm.exe",
        "taskmgr.exe",
        "applicationframehost.exe",
        "shellexperiencehost.exe",
        "startmenuexperiencehost.exe",
        "searchhost.exe",
        "searchapp.exe",
        "pickerhost.exe",
        "lockapp.exe",
        "rundll32.exe",
        "systemsettings.exe",
        "openwith.exe",
        "consent.exe",
        "credentialuibroker.exe",
        "msiexec.exe",
        "setup.exe",
        "irsetup.exe",
        "python.exe",
        "pythonw.exe",
        "py.exe",
        "conhost.exe",
        "steamwebhelper.exe",
        "textinputhost.exe",
        "phoneexperiencehost.exe",
        "windowsterminal.exe",
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "werfault.exe",
        "electron.exe",  # her own avatar runs as Electron
    }
)
_INSTALLER = re.compile(r"(?:setup|install|update|uninst|\.tmp)", re.IGNORECASE)


@dataclass(frozen=True)
class AskTool:
    """A read-only tool she may offer to use again: what it gets, said as an offer."""

    what: str  # "the weather"
    question: str  # "Want it?"
    domain: str  # the Futures domain (off-limits domains are never offered)


ASK_TOOLS: dict[str, AskTool] = {
    "get_weather": AskTool("the weather", "Want it?", "general"),
    "get_calendar_events": AskTool("your calendar", "Want a look at it?", "productivity"),
    "canvas_due": AskTool("what's due on Canvas", "Want to hear it?", "school"),
    "ticktick_list_undone_tasks_by_time_query": AskTool("your to-dos", "Want them?", "productivity"),
}
APP_TOOL = "open_application"  # how an app is opened on a yes (SAFE: it only starts an app)
APP_DOMAIN = "computer"
DOMAIN = "routines"  # the Futures domain of every offer: "routines" off-limits stops all of it


def norm_process(process: str) -> str:
    return (process or "").strip().lower()


def may_learn_app(process: str, app: str, excluded: Iterable[str] = ()) -> bool:
    """Whether the app may be learned (and offered): not Windows' own, not an installer, not a password manager or
    an app the user keeps out of screen history (the built-in list and ``excluded``), and it has a name to say."""
    from octavia.recall.privacy import excluded_app

    name = norm_process(process)
    if not name or not (app or "").strip() or name in SYSTEM_PROCESSES or _INSTALLER.search(name):
        return False
    return not excluded_app(name, excluded)


def clock(minute: int) -> str:
    """Minutes since the day's start (``DAY_START_H``) as it's said: "9:05 PM"."""
    total = (DAY_START_H * 60 + int(minute)) % (24 * 60)
    hour, mins = divmod(total, 60)
    suffix = "AM" if hour < 12 else "PM"
    return f"{(hour % 12) or 12}:{mins:02d} {suffix}"


def app_time_offer(app: str) -> str:
    return f"You usually open {app} around now. Want me to open it?"


def app_after_offer(app: str, after: str) -> str:
    return f"You usually open {app} after {after}. Want me to open it too?"


def ask_time_offer(tool: str) -> str:
    ask = ASK_TOOLS[tool]
    return f"You often ask me for {ask.what} around this time. {ask.question}"
