"""What she says she did, checked against what her tools did, and what she says she'll do, kept (2026-10-25 review).

The user caught her more than once: "didn't you say you were gonna install them one at a time once that one
failed?", "did you actually read through all the different PDFs?", "I thought you said you had fixed these". The local
brain's claims are checked (``localbrain.delegation.CLAIMED``) and tasks end [done] or [unfinished], but nothing looked
at Claude's own spoken turns.

Deterministic, no model: after each of the user's turns the brain answers (``BrainFinished``),

* **claims**: a sentence saying she did something ("I've installed it", "I sent the email", "I read all the PDFs") is
  matched against the tools that succeeded in that turn or the few before it (``ToolCallFinished``). A claim no
  successful tool backs is *unbacked*;
* **promises**: a sentence saying she'll do something later or on a condition ("I'll install them one at a time once
  that one finishes", "I'll remind you tonight") is a promise, kept in ``data/promises.json`` until it ages out. One
  for a time on the calendar ("I'll ping you Tuesday mornings", "from now on I'll check") needs something that wakes
  her then (a scheduled job, a timer, a reminder) set up in that turn or the few before; without one she's told on
  her next turn to set it up or say it isn't (unless a job she already has covers it).

``claims.mode``: ``log`` writes both to ``data/claims.jsonl`` only; ``note`` also tells the brain on the next turn (an
unbacked claim: check it and correct yourself if it didn't happen; the promises still open), so it can own up instead
of the user having to catch it; ``off``.

``enforce`` (the default since 2026-10-26: 7 of her first 10 logged claims were unbacked, mostly "I sent it" after a
click and typing) does what ``note`` does, and also vets every sentence before it's spoken or shown (``vet``): a claim
to have sent, submitted, posted, clicked, opened, typed or finished something that her own steps contradict is said
honestly instead ("I pressed Enter to send it, but I couldn't confirm it went through."), and she's told on her next
turn what the user heard. A send on the screen counts when the typing tool saw the text leave the box
(``computer_control``'s ``sent``), a click on a send button changed something, or she looked at the screen after it;
an app's own send tool (Gmail, Discord) and her screen and web tasks (which end [done] or [unfinished]) count by
succeeding. Only what the steps positively contradict is rewritten (Enter pressed but nothing confirmed, the text
still in the box, typed but never sent, no step at all, the turn's last step failed); a claim that is merely
unmatched is logged and noted as before. A few regexes per sentence: nothing waits on it.

Her overnight work (``octavia/proactive``) is held to more, whatever the mode short of ``off``: a sentence saying she
prepared, drafted, wrote or researched something overnight (or the like) counts only when an item of that work passed
its check lately (``proactive``: their titles) and the sentence names it; otherwise she's told on her next turn to
correct herself, even in ``log`` mode. Work nobody watched is exactly where "done" must be checked.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from octavia.core.events import BrainFinished, BrainStarted, ClaimCorrected, EventBus, ToolCallFinished
from octavia.tts.chunker import clean_for_speech

log = logging.getLogger(__name__)

USER_SOURCES = frozenset({"voice", "text", "discord", "window"})
RECENT_TURNS = 3  # a claim may be about a tool that succeeded this many turns back ("I installed it" after the install)
PROMISE_DAYS = 3.0  # a promise is kept (and reminded of) this long
MAX_PROMISES = 20
_CLIP = 300

# What a send on the screen came to, as the typing tools tell it (``ToolResult.data["sent"]``) and say it to the model.
SENT_WENT = "went"  # the text left the box afterwards (or the focus moved on: a form went)
SENT_STUCK = "still_there"  # the text was still in the box afterwards
SENT_UNKNOWN = "unknown"  # the box doesn't say what's in it
SAID_SENT = {
    SENT_WENT: "The text left the box afterwards, so it went.",
    SENT_STUCK: (
        "The text is still in the box afterwards, so it may not have sent: look at the screen before saying it went."
    ),
    SENT_UNKNOWN: "I couldn't tell whether it went: look at the screen before saying it was sent.",
}

# What she may say she did, and which of her tools (by words in their names) can back it. A claim matches a sentence
# in the first person, past or perfect: "I installed", "I've sent", "I just read".
_BACKERS: dict[str, tuple[str, ...]] = {
    "installed": ("install",),
    "built": ("build_improvement", "start_objective"),
    "sent": ("send", "reply", "forward", "post"),
    "emailed": ("gmail_send", "send"),
    "texted": ("send", "discord"),
    "posted": ("post", "send"),
    "deleted": ("delete", "remove", "forget", "trash", "reject_improvement", "cancel"),
    "removed": ("delete", "remove", "forget", "trash", "reject_improvement", "cancel"),
    "saved": ("remember", "save", "note", "write", "correct_memory", "study_update", "create"),
    "remembered": ("remember", "correct_memory"),
    "noted": ("note_improvement", "remember", "study_update", "quiz_record"),
    "recorded": ("record", "remember", "study_update", "note", "save"),
    "added": ("add_", "create", "remember", "study_update", "note"),
    "created": ("create", "add_", "make_", "write"),
    "scheduled": ("schedule", "timer", "reminder", "add_calendar_event", "create"),
    "booked": ("add_calendar_event", "create"),
    "opened": ("open", "launch", "start_screen_task", "click", "show_", "practice_", "worksheet", "concept_map"),
    "played": ("play", "media_control", "spotify"),
    "paused": ("pause", "media_control", "spotify"),
    "submitted": ("click", "press_keys", "submit", "screen_task", "web_task"),
    "clicked": ("click", "screen_task", "web_task"),
    "typed": ("type_text", "screen_task", "web_task"),
    "read": ("read", "canvas", "open_file", "look_at_screen", "gmail", "search", "recall", "find_files", "study"),
    "checked": ("read", "canvas", "look", "gmail", "search", "list", "status", "get_", "recall", "garmin", "study"),
    "looked": ("look", "zoom", "find_on_screen", "read", "canvas", "search", "recall"),
    "downloaded": ("download", "long_command"),
    "restarted": ("restart",),
    "started": ("start", "build_improvement", "timer", "play"),
    "fixed": ("install", "build_improvement", "correct_memory", "type_text", "screen_task", "update"),
    "updated": ("update", "correct_memory", "set_", "study_update", "install"),
    "changed": ("set_", "update", "correct_memory", "change", "install"),
    "turned": ("set_", "media_control", "volume", "mute"),
    "set": ("set_", "timer", "reminder", "schedule", "volume", "create"),
    "finished": (),  # held to the turn's own steps (``_judge_done``)
    "completed": (),
}
_CLAIM = re.compile(
    r"\bI(?:'ve| have)?\s+(?:just\s+|already\s+|now\s+|also\s+|finally\s+|then\s+|went ahead and\s+)?"
    r"(" + "|".join(_BACKERS) + r")\b",
    re.IGNORECASE,
)
# ...and what she says she did next in the same breath: "I typed out all ten ideas and sent them to Claude".
_ALSO = re.compile(
    r"(?:\band|\bthen|,)\s+(?:then\s+|also\s+|just\s+|finally\s+)?(sent|submitted|posted|emailed|texted|clicked|"
    r"opened|typed)\b",
    re.IGNORECASE,
)
# A send said without "I" doing it: "Your message to Sam has been sent.", "All ten are sent.", "I got it sent.",
# "It went through." (not "...was sent by Dr. Liu": someone else's).
_SENT_SAID = re.compile(
    r"\b(?:has|have|had|was|were|is|are)\s+(?:been\s+|now\s+|all\s+)?(sent|submitted|posted)\b(?!\s+by\b)|"
    r"\bI(?:'ve)?\s+got\s+(?:it|them|that|this|those|\w+)\s+(sent|submitted|posted)\b|"
    r"\b(?:it|that|this|they|the message|your message|everything)\s+went\s+through\b",
    re.IGNORECASE,
)
# Someone else's words, said on: "He said: I sent them this morning", "Sam wrote back that I submitted it".
_REPORTED = re.compile(
    r"\b(\w+)\s+(?:said|says|wrote|writes|replied|replies|asked|asks|answered|texted)(?:\s+back)?\s*(?::|,|\bthat\b)",
    re.IGNORECASE,
)
_NOT_SOMEONE = frozenset({"i", "and", "then", "also", "just", "she", "octavia"})  # "...and asked it to rank them"
# A sentence with these isn't saying it happened: a question, a condition, a plan, a denial or a report of a failure,
# what she'd do or does as a habit ("Every month I'd reread my notes and notice how I've changed"), a guess ("The line
# breaks may have sent it early") or a check ("Checking whether it went through.").
_NOT_A_CLAIM = re.compile(
    r"\?|\b(?:if|when|once|unless|would|could|should|might|didn't|did not|couldn't|could not|haven't|have not|"
    r"wasn't|can't|cannot|not|never|tried|try|failed|wanted|meant|thought|before|earlier|yesterday|last time|"
    r"I'd|every (?:day|week|month|year|time)|each (?:day|week|month|time)|usually|always|"
    r"whether|may|maybe|perhaps|probably|possibly|seems|seemed|looks like|checking|check)\b",
    re.IGNORECASE,
)
# Quoted words are someone's words, not her claim now ('seven of my first ten "I sent it" claims weren't backed').
_QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”|‘[^’]*’|(?<![\w])'[^']*'(?![\w])")
# "I started talking about two and a half seconds in" says how something went, not that she started a thing.
_HOW_IT_WENT = re.compile(r"\b(?:started|began)\s+\w+ing\b", re.IGNORECASE)
# "Done." / "All done!" / "That's done." / "All set.": what she was asked is finished. Held to the turn's own steps.
_DONE = re.compile(
    r"^(?:(?:okay|ok|alright|and)\b,?\s+)?(?:all\s+)?(?:done|finished)\b|^all set\b|"
    r"^(?:that's|it's|that is|it is|everything's|everything is)\s+(?:all\s+)?(?:done|finished|sent|submitted)\b",
    re.IGNORECASE,
)
# "Sent it!", "Okay, submitted.", "It's sent.", "Message sent.": a send said short, with no "I".
_SENT_SHORT = re.compile(
    r"^(?:(?:okay|ok|alright|done|and)\b,?\s+)?(?:just\s+|all\s+)?(sent|submitted|posted)\b|"
    r"^(?:it's|that's|it has|it's been|that's been|it was|your message is|your message was|the message is|"
    r"the message was|message)\s+(?:been\s+|now\s+|all\s+)?(sent|submitted|posted)\b",
    re.IGNORECASE,
)
# "I'll do X once/when/after Y", or at a time: a promise she has to keep later. "I'll tell you when it's ready" isn't
# one: her builds, tasks and jobs announce themselves (half of her promises on 2026-10-25 were that).
_PROMISE = re.compile(
    r"\b(?:I'll|I will|I'm going to|I am going to|I'm gonna)\s+"
    r"(?!(?:also\s+)?(?:be|have|need|see|think|try|tell|let you know|ping|say|speak up|keep you posted|update you)\b)"
    r"[^.!?]*?\b(?:once|when|after|as soon as|tonight|tomorrow|later|this (?:afternoon|evening|morning)|"
    r"in (?:a|an|\d+) (?:minute|minutes|hour|hours|day|days)|at \d|one at a time|next time)\b[^.!?]*",
    re.IGNORECASE,
)
# A time on the calendar: "tonight", "tomorrow", "at 5", "Tuesday mornings", "every week", "on Tuesdays". She's only
# there then if something wakes her (``_WAKES``): nothing she says does. 2026-10-27 over Discord she said "From now on
# I'll check that class on Tuesdays and ping you" and "I'll ping you Tuesday mornings", set nothing up, and neither was
# even kept as a promise. A day is a time only said as one ("on Tuesday", "Tuesdays", "every Tuesday", "Tuesday
# mornings"): "what's due Tuesday" isn't.
_DAY = r"(?:mon|tues|wednes|thurs|fri|satur|sun)day"
_PART = r"(?:mornings?|afternoons?|evenings?|nights?)"
_LATER = re.compile(
    rf"\b(?:(?:tonight|tomorrow)(?!['’]s)|this (?:afternoon|evening|morning|weekend)|"
    rf"in (?:a|an|\d+|a few|a couple of|two|three) (?:hours?|days?|weeks?)|at \d|at (?:noon|midnight)|"
    rf"(?:on|every|each|next) {_DAY}s?(?: {_PART})?|{_DAY}s(?: {_PART})?|{_DAY} {_PART}|"
    rf"(?:every|each) (?:day|morning|afternoon|evening|night|week|weekday|month)|next (?:week|month)|"
    rf"in the morning)\b",
    re.IGNORECASE,
)
# ...to reach out or look then: what only happens if she's woken. Not "from now on I'll talk about them as my brains" or
# "I'll leave LLAB off your grade reports": how she'll behave, which needs nothing to wake her (seven of her fourteen
# promises with a time from 2026-10-07 to 10-28 were those).
_WILL = re.compile(
    r"\b(?:I['’]ll|I will|I['’]m going to|I am going to|I['’]m gonna)\s+(?:also\s+|just\s+|then\s+|first\s+)?"
    r"(?:ping|remind|text|message|nudge|bug|check|look|ask|follow up|tell|let you know|go through|send|call|bring|"
    r"warn|wake|update you|email)\b",
    re.IGNORECASE,
)
# What wakes her at a later time: a scheduled job, a timer, a reminder of hers, a to-do or event with a time.
_WAKES = re.compile(
    r"^(?:create|edit)_scheduled_job$|^set_timer$|^remind_me_when$|calendar_event$|^ticktick_(?:create|update|add)|"
    r"^start_screen_watch$"
)
_UNSET_NOTE = (
    "in your last reply you said {said}, but nothing you set up will wake you then: you only act later when a "
    "scheduled job (create_scheduled_job), a timer, remind_me_when or a TickTick task with a time does. Unless a job "
    "you already have covers it (list_scheduled_jobs; your morning rundown runs by itself), set one up now, or tell "
    "the user plainly that it isn't set up"
)
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
_SENTENCES_KEPT = re.compile(r"((?<=[.!?])\s+|\n+)")  # the same, with what's between them kept
# A claim about work she did on her own, beside the conversation, while nobody watched.
_OVERNIGHT = re.compile(
    r"\b(?:overnight|over night|last night|while you (?:were )?(?:slept|sleeping|asleep|away|out|gone))\b",
    re.IGNORECASE,
)
# ...said like this: only in a sentence about overnight work (``_OVERNIGHT``), so "I made a mistake" is no claim.
_OVERNIGHT_CLAIM = re.compile(
    r"\bI(?:'ve| have)?\s+(?:just\s+|already\s+|also\s+)?(prepared|drafted|wrote|written|researched|created|made|"
    r"put together|built|finished|did)\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z0-9]{2,}")
_COMMON = frozenset({"the", "and", "for", "you", "your", "with", "from", "that", "this", "of", "to", "on", "in", "at",
                     "my", "an", "it", "is", "be", "we", "me", "up", "as", "or", "by"})  # fmt: skip

# Sends and the like: on the screen they're held to what confirmed them (``_judge_send``).
SEND_VERBS = frozenset({"sent", "emailed", "texted", "posted", "submitted"})
_SUBMITS = frozenset({"submitted"})
# Claims her steps can positively contradict, so ``enforce`` says them honestly instead (beside the sends).
_ACT_VERBS = frozenset({"clicked", "opened", "typed"})
_DONE_VERBS = frozenset({"done", "finished", "completed"})
# Her hands on the screen: never an app's own send.
SCREEN_ACTS = frozenset({"click", "type_text", "press_keys", "scroll", "drag"})
_SEND_KEYS = frozenset({"enter", "ctrl+enter", "alt+s"})
# A look at the screen after a send is her checking it went (she saw it).
# So is a read of the app it went to ("it shows up in the channel now" after reading the channel).
_LOOKS = frozenset({"look_at_screen", "zoom_screen", "find_on_screen", "wait_for_screen_change", "see_screen_change",
                    "discord_read_messages", "read_google_chat", "gmail_recent", "search_gmail", "web_read",
                    "web_screenshot", "check_web_task"})  # fmt: skip
_SEND_BUTTON = re.compile(r"\b(?:send|submit|post|reply|publish|tweet|share)\b", re.IGNORECASE)
# Starting work isn't finishing it: a screen task that started hasn't sent anything yet.
_STARTS = re.compile(r"^(?:start|resume|list|cancel|stop)_")
# Reads and looks: a failed one ends no task ("Done" after a find that came up empty, then a click that worked).
_READS = re.compile(r"^(?:look|zoom|find|wait|list|get|read|search|recall|check|whats|what)")
# Steps on the side of a task: one of these failing at the end doesn't undo it ("Done." after a panel that didn't open).
_SIDE = re.compile(r"^(?:show_|close_|update_visual|scroll|remember|point_|mark_|clear_|guide_on_screen|play_sound|"
                   r"note_improvement|press_keys)")  # fmt: skip

# A change to one of her own panels, said as such: "I added the rule and each one's neighbors to the panel." Her
# panel tools back it, whatever the verb (2026-11-21 an update_visual in the same turn didn't, and a note told her
# it never happened).
_ON_PANEL = re.compile(r"\b(?:panels?|on (?:the|your) screen|up there|on there)\b", re.IGNORECASE)
_PANEL_TOOLS = re.compile(
    r"(?:^|__)(?:show_visual|update_visual|explain|show_image|show_page|bring_back_visual|pin_visual|close_visual|"
    r"concept_map|simulate|practice_worksheet)$"
)
_PANEL_VERBS = frozenset({"added", "created", "updated", "changed", "removed", "opened", "set", "fixed", "turned"})

_OVERNIGHT_NOTE = (
    "in your last reply you said {said}, but no overnight work of yours that passed its check matches it "
    "(overnight_work lists what did). Say plainly now what was and wasn't done, and never call unchecked work done"
)
_REWRITTEN_NOTE = (
    "in your last reply you said {said}, but {why}, so the user heard this instead: {heard}. Don't say it went until "
    "something shows it did (look at the screen, or the app's own confirmation); if it matters, check now and tell "
    "them what you see"
)
_WHY = {
    SENT_STUCK: "the text was still in the box after you pressed Enter",
    "unconfirmed": "nothing confirmed the send (the box didn't say, and you didn't look afterwards)",
    "typed_only": "you only typed it: no Enter or send button was pressed",
    "nothing": "every tool you used in this turn and the ones just before failed",
    "last_failed": "the last step of this turn failed",
    "failed": "the tool that would have done it failed in this turn",
}


@dataclass
class Claim:
    verb: str
    sentence: str
    backed_by: str = ""  # the tool that backs it, or "" (unbacked)
    # Why her steps contradict it, when they do (``enforce`` says it honestly instead): "unconfirmed" (a send on the
    # screen nothing confirmed), "still_there" (the text was still in the box after Enter), "typed_only" (typed, never
    # sent), "nothing" (every step of hers lately failed), "last_failed" (the turn's last step failed); "" otherwise.
    why: str = ""
    said_as: str = ""  # what she said instead (``enforce``)
    # The send on the screen the verdict rests on: type_text, press_keys, click (a send button) or "click after
    # typing" (an unnamed one).
    step: str = ""


@dataclass
class Promise:
    text: str
    at: float
    turn: str = ""  # what the user had said
    reminded: bool = False


@dataclass(frozen=True)
class Act:
    """One of her tool calls that finished: what it was, whether it worked, and what it said it did."""

    name: str
    ok: bool
    data: Mapping[str, Any] | None = None
    summary: str = ""
    turn: int = 0  # 0: this turn; 1: the one before, and so on


def claims_of(sentence: str) -> list[Claim]:
    """What ``sentence`` says she did, each thing with its verb ([] when it says she did nothing)."""
    sentence = sentence.strip()
    bare = _QUOTED.sub("", sentence)
    if _NOT_A_CLAIM.search(bare) or _HOW_IT_WENT.search(bare):
        return []
    if any(m.group(1).lower() not in _NOT_SOMEONE for m in _REPORTED.finditer(bare)):
        return []  # someone else's words
    clip = sentence[:_CLIP]
    verbs: list[str] = []
    if first := _CLAIM.search(bare):
        verbs += [m.group(1).lower() for m in _CLAIM.finditer(bare)]
        verbs += [m.group(1).lower() for m in _ALSO.finditer(bare, first.end())]
    verbs += [("sent" if m.lastindex is None else m.group(m.lastindex).lower()) for m in _SENT_SAID.finditer(bare)]
    start = bare.lstrip("*_ ")
    if not verbs and (match := _SENT_SHORT.search(start)):
        verbs.append((match.group(1) or match.group(2)).lower())
    if not verbs and _DONE.search(start):
        verbs.append("done")
    return [Claim(verb, clip) for verb in dict.fromkeys(verbs)]


def claim_in(sentence: str) -> Claim | None:
    """The first thing ``sentence`` says she did, if it says she did something."""
    return next(iter(claims_of(sentence)), None)


def claims_in(reply: str) -> list[Claim]:
    """What the sentences of ``reply`` say she did, each with its verb."""
    return [claim for sentence in _SENTENCES.split(reply) for claim in claims_of(sentence)]


def could_claim(text: str) -> bool:
    """Whether ``text`` (maybe half a sentence) says she did something: the speech stream holds such a piece until
    its sentence is whole, so a question's first half ("Do you want me to check whether I sent it,") isn't judged."""
    return bool(claims_of(text))


def promises_in(reply: str) -> list[str]:
    """The sentences of ``reply`` that promise something for later or on a condition."""
    found = []
    for sentence in _SENTENCES.split(reply):
        sentence = sentence.strip()
        if (_PROMISE.search(sentence) or timed(sentence)) and "?" not in sentence:
            found.append(sentence[:_CLIP])
    return found


def timed(promise: str) -> bool:
    """Whether ``promise`` is for a time on the calendar (``_LATER``), when only something she set up wakes her."""
    bare = _QUOTED.sub("", promise)
    return "?" not in bare and bool(_WILL.search(bare)) and bool(_LATER.search(bare))


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.casefold()) if w not in _COMMON}


def overnight_backer(sentence: str, verified: list[str]) -> str:
    """The checked overnight item a claim names, by its subject ("ITCS 6150 AI Midterm", the to-do's own words, the
    project): two of its words (one, for a one-word subject), so "a review sheet for your Math 200 midterm" isn't
    backed by the one for CS 101's."""
    said = _words(sentence)
    for subject in verified:
        words = _words(subject)
        if words and len(said & words) >= min(2, len(words)):
            return f"overnight: {subject}"
    return ""


def overnight_claims_in(reply: str) -> list[Claim]:
    """The sentences of ``reply`` saying she did work overnight, while the user was away."""
    found = []
    for sentence in _SENTENCES.split(reply):
        sentence = sentence.strip()
        match = _OVERNIGHT_CLAIM.search(sentence)
        if match and _OVERNIGHT.search(sentence) and not _NOT_A_CLAIM.search(sentence):
            found.append(Claim(match.group(1).lower(), sentence[:_CLIP]))
    return found


def backer(verb: str, tools: list[str]) -> str:
    """The first of ``tools`` (names of tools that succeeded) that can back ``verb``, or ""."""
    words = _BACKERS.get(verb, ())
    return next((tool for tool in tools if any(word in tool for word in words)), "")


def sent_outcome(act: Act) -> str:
    """What a send on the screen came to: ``SENT_WENT``, ``SENT_STUCK`` or ``SENT_UNKNOWN``. From the tool's own
    ``sent`` (type_text and press_keys look at the box), what a click on a send button changed, or else what its
    result says (the eval's canned results have only words)."""
    data = act.data or {}
    if (sent := data.get("sent")) in SAID_SENT:
        return str(sent)
    if act.name == "click" and "changed" in data:
        return SENT_WENT if data.get("changed") else SENT_UNKNOWN
    said = act.summary.casefold()
    for outcome in (SENT_STUCK, SENT_WENT):
        if SAID_SENT[outcome].casefold()[:40] in said:
            return outcome
    return SENT_UNKNOWN


def _screen_send(act: Act) -> bool:
    """Whether ``act`` (it worked) is a send on the screen: typing with Enter, Enter pressed, a send button clicked."""
    data = act.data or {}
    if act.name == "type_text":
        return bool(data.get("enter")) or (not data and "pressed enter" in act.summary.casefold())
    if act.name == "press_keys":
        return str(data.get("keys", "")) in _SEND_KEYS or (not data and "pressed enter" in act.summary.casefold())
    if act.name == "click":
        return bool(_SEND_BUTTON.search(str(data.get("name", "")) or act.summary))
    return False


def _judge_send(claim: Claim, acts: list[Act]) -> None:
    """A send is backed by an app's own send tool or a task of hers that succeeded, or by a send on the screen that
    something confirmed: the box emptied, the click changed something, or she looked after it. Any send in the window
    confirmed backs it (which message a claim means can't be told). Contradicted when none was."""
    words = tuple(w for w in _BACKERS.get(claim.verb, ()) if w not in ("click", "press_keys"))
    for act in reversed(acts):
        if act.ok and act.name not in SCREEN_ACTS and not _STARTS.match(act.name) and any(w in act.name for w in words):
            claim.backed_by = act.name
            return
    sends = [i for i, act in enumerate(acts) if act.ok and _screen_send(act)]
    typed = [i for i, act in enumerate(acts) if act.ok and act.name == "type_text"]
    after_typing: set[int] = set()
    if typed and not _screen_send(acts[typed[-1]]):  # typed without Enter: a click after it may be its send button
        after_typing = {i for i in range(typed[-1] + 1, len(acts)) if acts[i].ok and acts[i].name == "click"}
        sends = sorted({*sends, *after_typing})
    if not sends:
        if typed and not any(a.ok and not _READS.match(a.name) for a in acts[typed[-1] + 1 :]):
            claim.why = "typed_only"
        elif acts and not any(act.ok for act in acts):
            claim.why = "nothing"
        return
    for i in reversed(sends):
        if sent_outcome(acts[i]) == SENT_WENT:
            claim.backed_by = f"{acts[i].name}: confirmed"
            return
        if look := next((a.name for a in acts[i + 1 :] if a.ok and a.name in _LOOKS), ""):
            claim.backed_by = f"{look} after {acts[i].name}"
            return
    last = sends[-1]
    claim.step = "click after typing" if last in after_typing and not _screen_send(acts[last]) else acts[last].name
    claim.why = SENT_STUCK if sent_outcome(acts[last]) == SENT_STUCK else "unconfirmed"


def _judge_done(claim: Claim, acts: list[Act]) -> None:
    """ "Done" is about this turn's steps: backed when its last step (reads and looks aside) worked, contradicted when
    it failed. A turn with no steps isn't a claim about any (``ClaimWatch`` drops it)."""
    steps = [a for a in acts if a.turn == 0 and not _READS.match(a.name)]
    if not steps:
        return
    while len(steps) > 1 and not steps[-1].ok and _SIDE.match(steps[-1].name):
        steps.pop()  # a side step that failed after the work (a panel, a note, Escape) doesn't undo it
    if steps[-1].ok:
        claim.backed_by = steps[-1].name
    else:
        claim.why = "last_failed"


def judge(claim: Claim, acts: list[Act]) -> Claim:
    """``claim`` with ``backed_by`` (and ``why``, when her steps contradict it) from ``acts``, oldest first."""
    if claim.verb in SEND_VERBS:
        _judge_send(claim, acts)
    elif claim.verb in _DONE_VERBS:
        _judge_done(claim, acts)
    else:
        newest_first = [a.name for a in sorted(acts, key=lambda a: a.turn) if a.ok]
        claim.backed_by = backer(claim.verb, newest_first)
        if not claim.backed_by and claim.verb in _PANEL_VERBS and _ON_PANEL.search(claim.sentence):
            claim.backed_by = next((name for name in newest_first if _PANEL_TOOLS.search(name)), "")
        if not claim.backed_by and claim.verb in _ACT_VERBS and acts and not any(a.ok for a in acts):
            claim.why = "nothing"
        elif not claim.backed_by and backer(claim.verb, [a.name for a in acts if a.turn == 0 and not a.ok]):
            claim.why = "failed"  # "I've recorded it" said before a call that then failed: the call says otherwise
    return claim


def honest(claim: Claim, acts: list[Act]) -> str:
    """What she says instead of ``claim``, which her steps contradict (``claim.why``)."""
    if claim.why in (SENT_STUCK, "unconfirmed"):
        aim = "submit" if claim.verb in _SUBMITS else "send"
        did = {"click": f"I clicked {aim}", "click after typing": f"I clicked to {aim} it"}.get(
            claim.step, f"I pressed Enter to {aim} it"
        )
        if claim.why == SENT_STUCK:
            return f"{did}, but the text is still in the box, so it doesn't look like it went through."
        return f"{did}, but I couldn't confirm it went through."
    if claim.why == "typed_only":
        return "I typed it in, but I haven't sent it yet."
    if claim.why == "last_failed":
        return "That last step didn't go through, so it isn't done yet."
    return f"Actually, I haven't {claim.verb} that yet."


@dataclass
class _Turn:
    text: str = ""
    source: str = "voice"
    acts: list[Act] = field(default_factory=list)  # finished in this turn, worked or not
    rewritten: dict[str, Claim] = field(default_factory=dict)  # by the sentence's words: said honestly instead

    @property
    def tools(self) -> list[str]:  # succeeded in this turn
        return [a.name for a in self.acts if a.ok]


def _key(sentence: str) -> str:
    """A sentence by its words as spoken, so the piece the voice said and the whole reply shown match."""
    return " ".join(_WORD.findall(clean_for_speech(sentence).casefold()))


class ClaimWatch:
    """Listens on the bus, checks each brain turn's claims and keeps its promises (see the module's doc)."""

    def __init__(
        self,
        events: EventBus,
        log_path: Path,
        promises_path: Path,
        *,
        mode: str = "enforce",
        note: Callable[[str], None] | None = None,
        proactive: Callable[[], list[str] | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.log_path = log_path
        self.promises_path = promises_path
        self.mode = mode
        self.note = note
        self._proactive = proactive  # titles of overnight work that passed its check lately (None: not running)
        self._clock = clock
        self._turn: _Turn | None = None
        self._recent: deque[list[Act]] = deque(maxlen=RECENT_TURNS)
        self._loose: list[Act] = []  # her tools run outside a brain turn (a quick command): a turn of their own
        self._events = events
        events.subscribe(self._on_event, BrainStarted, ToolCallFinished, BrainFinished)

    def _on_event(self, event: Any) -> None:
        try:
            match event:
                case BrainStarted(text=text, source=source):
                    if self._loose:
                        self._recent.append(self._loose)
                        self._loose = []
                    self._turn = _Turn(text=text, source=source)
                case ToolCallFinished(name=name, ok=ok, data=data, summary=summary):
                    (self._turn.acts if self._turn is not None else self._loose).append(Act(name, ok, data, summary))
                case BrainFinished(text=reply, source=source, error=None, interrupted=False, turn_id=turn_id):
                    turn, self._turn = self._turn, None
                    if turn is None:
                        return
                    if source in USER_SOURCES and (reply or turn.rewritten):
                        self.check(turn, reply)
                    for claim in list(turn.rewritten.values())[:1]:  # what lessons keep of it (``futures.lessons``)
                        self._events.emit(ClaimCorrected(sentence=claim.sentence, turn_id=turn_id))
                    self._told(turn)  # her own turns (a reminder, a task's report) too
                    self._recent.append(turn.acts)
                case BrainFinished():
                    if self._turn is not None:
                        if self._turn.rewritten:  # cut off or failed, but what she said instead was heard
                            self._told(self._turn)
                        self._recent.append(self._turn.acts)
                    self._turn = None
        except Exception:  # a check of her words must never break a turn
            log.warning("Checking her claims failed", exc_info=True)

    def _acts(self, turn: _Turn) -> list[Act]:
        """Her steps in ``turn`` and the few turns before it, oldest first, each marked with how many turns back."""
        recent = [*self._recent, self._loose] if self._loose else list(self._recent)
        older = [
            Act(a.name, a.ok, a.data, a.summary, turn=back)
            for back, acts in zip(range(len(recent), 0, -1), recent, strict=True)
            for a in acts
        ]
        return [*older, *turn.acts]

    def earlier(self) -> list[Act]:
        """Her calls in the few turns before the one going on (each marked with how many turns back)."""
        turn = self._turn
        return [a for a in self._acts(turn if turn is not None else _Turn()) if a.turn > 0]

    # -- before it's said ----------------------------------------------------------------------------------------
    def vet(self, text: str) -> str:
        """``text`` (a piece of her reply about to be spoken, or the whole of it about to be shown), with each
        sentence claiming what her steps contradict said honestly instead (``enforce``). Unchanged otherwise."""
        turn = self._turn
        if self.mode != "enforce" or turn is None or not text:
            return text
        try:
            return self._vetted(turn, text)
        except Exception:  # never in the way of her answer
            log.warning("Vetting her claims failed", exc_info=True)
            return text

    def _vetted(self, turn: _Turn, text: str) -> str:
        acts: list[Act] | None = None
        pieces = _SENTENCES_KEPT.split(text)
        changed = False
        for i in range(0, len(pieces), 2):
            claims = claims_of(pieces[i])
            if not claims:
                continue
            key = _key(claims[0].sentence)
            if (said := turn.rewritten.get(key)) is None:
                acts = acts if acts is not None else self._acts(turn)
                # The sentence stands or falls by the first thing in it her steps contradict, sends first.
                judged = sorted((judge(c, acts) for c in claims), key=lambda c: c.verb not in SEND_VERBS)
                claim = next((c for c in judged if c.why), None)
                if claim is None:
                    continue
                claim.said_as = honest(claim, acts)
                turn.rewritten[key] = said = claim
                log.info("Saying %r instead of %r: %s", claim.said_as, claim.sentence, claim.why)
            pieces[i] = _spaced(pieces[i], said.said_as)
            changed = True
        return "".join(pieces) if changed else text

    # -- after it's said -----------------------------------------------------------------------------------------
    def check(self, turn: _Turn, reply: str) -> tuple[list[Claim], list[str]]:
        """Her ``reply`` to ``turn``: its claims (each backed or not) and its new promises; logged, and in ``note``
        and ``enforce`` modes told to the brain's next turn."""
        acts = self._acts(turn)
        verified = self._verified()
        # Overnight work is held to what passed its check, not to the turn's tools.
        overnight = overnight_claims_in(reply) if verified is not None else []
        for claim in overnight:
            claim.backed_by = overnight_backer(claim.sentence, verified or [])
        said_overnight = {c.sentence for c in overnight}
        claims = [judge(c, acts) for c in claims_in(reply) if c.sentence not in said_overnight]
        # "Done" with no step in the turn is no claim about any (thanks, an answer about something else).
        claims = [c for c in claims if c.verb not in _DONE_VERBS or c.backed_by or c.why]
        claims += overnight
        rewritten = list({c.said_as: c for c in turn.rewritten.values()}.values())
        promised = promises_in(reply)
        woken_by, unset = self._set_up(promised, acts)
        unbacked = [c for c in claims if not c.backed_by]
        unbacked_overnight = [c for c in overnight if not c.backed_by]
        if claims or promised or rewritten:
            self._log(
                {
                    "ts": round(self._clock(), 3),
                    "source": turn.source,
                    "said": turn.text[:_CLIP],
                    "tools": turn.tools,
                    "claims": [asdict(c) for c in [*rewritten, *claims]],
                    "unbacked": len(unbacked) + len(rewritten),
                    **({"rewritten": len(rewritten)} if rewritten else {}),
                    **({"overnight_unbacked": len(unbacked_overnight)} if overnight else {}),
                    "promises": promised,
                    **_wakes_fields(woken_by, unset),
                }
            )
        if promised:
            self._keep(promised, turn.text, settled=self._settled(promised, woken_by, unset))
        if unset:
            self._unset(unset)
        if unbacked_overnight and self.note is not None and self.mode not in ("note", "enforce"):
            said = "; ".join(f'"{c.sentence}"' for c in unbacked_overnight[:2])  # enforced in every mode but off
            self.note(_OVERNIGHT_NOTE.format(said=said))
        if self.mode in ("note", "enforce") and self.note is not None:
            if unbacked_overnight:
                said = "; ".join(f'"{c.sentence}"' for c in unbacked_overnight[:2])
                self.note(_OVERNIGHT_NOTE.format(said=said))
            self._told(turn)
            if unbacked := [c for c in unbacked if c not in unbacked_overnight]:
                said = "; ".join(f'"{c.sentence}"' for c in unbacked[:2])
                self.note(
                    f"in your last reply you said {said}, but none of your tools did that in this turn or the ones "
                    "just before. If it didn't happen, say so plainly now and don't claim it again; if it did happen "
                    "some other way, carry on"
                )
            if due := [p for p in self.open_promises() if not p.reminded and p.text not in promised]:
                self.note("promises you made and haven't kept yet: " + "; ".join(f'"{p.text}"' for p in due[:3]))
                self._write(self._marked(due[:3]))  # once each: a note on every turn would be noise
        return claims, promised

    @staticmethod
    def _set_up(promised: list[str], acts: list[Act]) -> tuple[str, list[str]]:
        """For ``promised``: the tool that will wake her for the ones at a time on the calendar ("" for none), and
        those that nothing will (all of them when no such tool worked in this turn or the few before)."""
        at_times = [p for p in promised if timed(p)]
        if not at_times:
            return "", []
        woken_by = next((a.name for a in reversed(acts) if a.ok and _WAKES.search(a.name)), "")
        return woken_by, [] if woken_by else at_times

    def _settled(self, promised: list[str], woken_by: str, unset: list[str]) -> set[str]:
        """The promises not to remind her of later: the ones something will wake her for, and the ones she's told
        about now (``_unset``). The rest are reminded of once, as before."""
        if woken_by:
            return {p for p in promised if timed(p)}
        return set(unset) if self.mode in ("note", "enforce") and self.note is not None else set()

    def _unset(self, unset: list[str]) -> None:
        log.info("Promised for later with nothing set up to wake her: %s", unset)
        if self.mode in ("note", "enforce") and self.note is not None:
            self.note(_UNSET_NOTE.format(said="; ".join(f'"{p}"' for p in unset[:2])))

    def _told(self, turn: _Turn) -> None:
        """Tells the brain's next turn what it said that the user heard otherwise, once."""
        if self.note is None or not turn.rewritten:
            return
        told = {c.said_as: c for c in turn.rewritten.values()}  # one note for what was said once, spoken and shown
        for claim in list(told.values())[:2]:
            self.note(
                _REWRITTEN_NOTE.format(
                    said=f'"{claim.sentence}"', why=_WHY.get(claim.why, claim.why), heard=f'"{claim.said_as}"'
                )
            )
        turn.rewritten = {}

    def _verified(self) -> list[str] | None:
        if self._proactive is None:
            return None
        try:
            return self._proactive()
        except Exception:
            log.warning("Couldn't read which overnight work passed its check", exc_info=True)
            return []

    def open_promises(self) -> list[Promise]:
        cutoff = self._clock() - PROMISE_DAYS * 86400
        return [p for p in self._read() if p.at >= cutoff]

    def _marked(self, reminded: list[Promise]) -> list[Promise]:
        """The open promises, with ``reminded`` marked as told."""
        texts = {(p.text, p.at) for p in reminded}
        kept = self.open_promises()
        for promise in kept:
            if (promise.text, promise.at) in texts:
                promise.reminded = True
        return kept

    def _keep(self, promised: list[str], said: str, settled: set[str] = frozenset()) -> None:
        now = self._clock()
        new = (Promise(text, now, said[:_CLIP], reminded=text in settled) for text in promised)
        kept = [*self.open_promises(), *new][-MAX_PROMISES:]
        self._write(kept)

    def _read(self) -> list[Promise]:
        try:
            rows = json.loads(self.promises_path.read_text(encoding="utf-8"))
            return [Promise(**{k: v for k, v in r.items() if k in Promise.__dataclass_fields__}) for r in rows]
        except (OSError, ValueError, TypeError):
            return []

    def _write(self, promises: list[Promise]) -> None:
        try:
            self.promises_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.promises_path.with_suffix(".tmp")
            tmp.write_text(json.dumps([asdict(p) for p in promises], indent=1), encoding="utf-8")
            tmp.replace(self.promises_path)
        except OSError:
            log.warning("Couldn't save her promises", exc_info=True)

    def _log(self, record: dict[str, Any]) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            log.warning("Couldn't write %s", self.log_path, exc_info=True)


def _wakes_fields(woken_by: str, unset: list[str]) -> dict[str, Any]:
    """For the log: what will wake her for a promise at a time, or the promises nothing will."""
    return {**({"set_up": woken_by} if woken_by else {}), **({"not_set_up": unset} if unset else {})}


def _spaced(original: str, said: str) -> str:
    """``said`` in place of the sentence ``original``, keeping the spaces around it."""
    lead = original[: len(original) - len(original.lstrip())]
    trail = original[len(original.rstrip()) :]
    return lead + said + trail
