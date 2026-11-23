"""Graded work is the user's to do: she never turns in a quiz, exam or test for them, and never works inside a locked
exam browser. The rest of the line (not answering a graded question at all) is hers to keep in what she says
(``personality/system.md``); this is the part code can hold, whatever she's asked or a page says.

* A click (or Enter or Space) on a button that turns in an attempt is refused, on any site: one named for it
  ("Submit Quiz", "Finish attempt", "Submit exam", Moodle's "Submit all and finish"), or a plain "Submit", "Save and
  Submit" or "Finish" in a window whose title is a quiz, exam or test. DataCamp's "Submit Answer" is none of these,
  so the exercises the user lets her work through go on as before; nor is "Submit exam request" or "End Test Run".
* Nothing at all is done in a proctoring or lockdown browser (Respondus LockDown Browser, Safe Exam Browser).
"""

from __future__ import annotations

import re

_ASSESSMENT = r"(?:quiz(?:zes)?|exams?|tests?|attempts?|knowledge checks?)"
# Words that make it something else: a request to sit an exam, a test run, results sent in, a test drive.
_NOT_IT = r"(?! (?:requests?|registrations?|runs?|results?|drives?|forms?|accommodations?|scores?|dates?|times?))"
_ENDS_ATTEMPT = re.compile(
    rf"\b(?:(?:submit|finish|turn in|hand in|end)(?: (?:the|my|this|your))? {_ASSESSMENT}{_NOT_IT}\b"
    r"|submit all and finish\b)",
    re.IGNORECASE,
)
# A button that turns in whatever page it's on, judged by the window it's in.
_GENERIC = re.compile(r"(?:save and )?submit(?: answers| all| now)?|finish|done|turn in", re.IGNORECASE)
_ASSESSMENT_WINDOW = re.compile(
    r"\b(?:quiz(?:zes)?|exams?|midterms?|finals? exam|(?:class |unit )?test \d+|knowledge checks?|assessments?)\b",
    re.IGNORECASE,
)
# Practice, and DataCamp (its exercises are the user's pre-approved steps, ``submit_sites``)
_PRACTICE_WINDOW = re.compile(r"\b(?:practice|datacamp)\b|\| (?:python|r|sql|shell|git|theory)\b", re.IGNORECASE)

LOCKDOWN_PROCESSES = frozenset(
    {
        "lockdownbrowser.exe",
        "lockdownbrowseroem.exe",
        "safeexambrowser.exe",
        "safeexambrowser.client.exe",
        "seb.exe",
        "lockdown browser.exe",
    }
)

ENDS_ATTEMPT = (
    "That button turns in a quiz or exam, and graded work is yours to turn in, so I won't press it. "
    "Tell the user to submit it themselves."
)
LOCKED_EXAM = (
    "That's a locked exam browser: whatever is open in it is graded work that's yours to do, so I won't act in it."
)


def ends_attempt(name: str, window: str = "") -> bool:
    """Whether a button called ``name``, in a window titled ``window``, turns in a quiz, exam or test attempt."""
    said = " ".join(name.split())
    if _ENDS_ATTEMPT.search(said):
        return True
    title = " ".join(window.split())
    return (
        bool(_GENERIC.fullmatch(said.rstrip(" .!>›»")))
        and bool(_ASSESSMENT_WINDOW.search(title))
        and not bool(_PRACTICE_WINDOW.search(title))
    )


def locked_exam(process: str) -> bool:
    """Whether ``process`` (an image name) is an exam lockdown browser."""
    return process.strip().casefold() in LOCKDOWN_PROCESSES
