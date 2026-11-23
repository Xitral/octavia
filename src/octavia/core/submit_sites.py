"""Sites where she submits without asking: DataCamp always (the user has said again and again that its Submit Answer,
Got It and Continue are pre-approved, in a conversation or a screen task alike), plus whatever the user lists in
``permissions.submit_without_asking``. A window is on a site when its title names it, and DataCamp's exercise pages
also when titled the way they are, for the exercise and its technology ("Hello Python! | Python").

Only a send or submit the tool's own look flagged (scope ``:send``) in a window whose title names the site: buying,
deleting, installing, passwords and commands there still ask. A high-risk place (``always_ask_before_sending``) wins
over it, and a click on a hint or the solution is never waved through, whatever it was flagged as. The mouse and
keyboard tools flag a click on one of the site's step buttons (``is_step_button``: Submit Answer, Continue, Run Code) as a send,
whatever else its surroundings made it look like (``computer_control._site_step``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from octavia.tools.permissions import ConfirmationRequest

ALWAYS_TRUSTED = ("DataCamp",)

# What the user never wants used on these sites (DataCamp's Hint, Take Hint, Show Answer): the same words the web
# tasks refuse (``_web_tasks.SITE_RULES``).
_OFF_LIMITS = re.compile(r"\bhints?\b|show (the )?(answer|solution)", re.IGNORECASE)

# The buttons that submit an exercise or move on to the next one. What's around them can make a click look like
# something else (a Continue under feedback that mentions running code reads as "runs something", Run Code outside
# what UI Automation calls the page, or the model's own "risky"), so on these sites they count as the submit they are.
_STEP_BUTTON = re.compile(
    r"(?:submit(?: answer| code)?|continue|got it|run code|next(?: exercise| lesson)?|check answer|try again"
    r"|keep going|proceed)\b"
    r"(?: to (?:the )?next (?:exercise|lesson|chapter))?[\s.!>›»]*",
    re.IGNORECASE,
)


# DataCamp's exercise pages are titled for the exercise and its technology, not the site ("Hello Python! | Python -
# Vivaldi", "Joining tables | SQL - Google Chrome"), so a window titled that way is DataCamp's too. The browser's
# name, or Edge's "and 3 more pages", may follow.
_DATACAMP_EXERCISE = re.compile(
    r"\S \| (?:Python|R|SQL|Shell|Git|Scala|Julia|Spreadsheets|Excel|Power BI|Tableau|Alteryx|Docker|Kubernetes"
    r"|AWS|Azure|Snowflake|Databricks|PySpark|ChatGPT|OpenAI|Theory)(?: - | and \d+ more|$)"
)


def is_step_button(name: str) -> bool:
    """Whether ``name`` is one of those buttons: "Submit Answer", "Got It!", "Continue to next exercise"."""
    return _STEP_BUTTON.fullmatch(" ".join(name.split())) is not None


def _named(window: str, places: Iterable[str]) -> bool:
    title = window.casefold()
    if _DATACAMP_EXERCISE.search(" ".join(window.split())):
        title += " | datacamp"
    return any(place.strip() and place.strip().casefold() in title for place in places)


def on_submit_site(window: str, sites: Iterable[str] = (), *, trusted: bool = True) -> bool:
    """Whether ``window``'s title names a site where she submits without asking: ``ALWAYS_TRUSTED`` or ``sites``
    (``trusted`` False: only ``sites``, as for the places that always ask)."""
    return bool(window) and _named(window, (*(ALWAYS_TRUSTED if trusted else ()), *sites))


def free_step(window: str, button: str, sites: Iterable[str] = (), always_ask: Iterable[str] = ()) -> bool:
    """Whether clicking ``button`` (its name) in ``window`` is a step she takes on such a site without asking: one of
    its step buttons (``is_step_button``), never a hint or the answer, not in an ``always_ask`` place."""
    if not is_step_button(button) or _OFF_LIMITS.search(button) or _named(window, always_ask):
        return False
    return on_submit_site(window, sites)


def submits_freely(request: ConfirmationRequest, sites: Iterable[str] = (), always_ask: Iterable[str] = ()) -> bool:
    """Whether ``request`` is a send or submit on a site where the user lets her submit without asking: one of
    ``ALWAYS_TRUSTED`` or ``sites``, and not one of the ``always_ask`` places."""
    if not request.window or not request.scope.endswith(":send"):
        return False
    if _OFF_LIMITS.search(f"{request.description} {request.args.get('label', '')}"):
        return False
    if _named(request.window, always_ask):
        return False
    return on_submit_site(request.window, sites)
