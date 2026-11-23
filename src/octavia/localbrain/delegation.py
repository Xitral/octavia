"""The local model as the first to hear a turn (``local_brain.mode`` "route"): in one reply it either answers, or hands
the turn to Claude with one line, ``DELEGATE: <why>``. Classifying and answering are one request, so a turn it keeps
costs no extra round trip, and a turn it hands over costs only a few tokens; its why goes in the route log.

What it's told is at the end of its system prompt (``instructions``), not in each turn, so the server keeps the whole
prompt cached. Its choice is never trusted alone: ``policy`` and ``required_note`` send some turns to Claude without
asking it, and the router checks every tool call and reply it makes (``router.RoutedBrain``). The shadow (mode
"shadow") asks it with the same prompt, so ``octavia shadow`` shows how often it would have handed a turn over, next to
what Claude did.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from typing import Any

from octavia.core.turn_types import TEXTED_NOTE

DELEGATE = "DELEGATE"
_DELEGATED = re.compile(rf"^\W*{DELEGATE}\b\W*(.*)", re.S)
_WHY_CHARS = 80

# Notes on a turn that are only context (by their first words, as ``Assistant._run_turn`` and its parts write them):
# the local model may still hear the turn, and is shown them. Any other note, known or not (interrupted, a request on
# hold, a turn that got stuck, something announced, a call, the screen, a tool's own note...), makes the turn Claude's.
_CONTEXT_NOTES = (
    'you already said "',  # the acknowledgment said out loud before the answer
    "from their voice (not their words) they sound",  # a guess at their tone
    "the user asked you to work quietly",
    "possibly related, looked up automatically",  # memories found for the words
    "on your mind since your last reflection",  # her own thoughts and opinions (inner life)
    "related to what they just said, a ",
    "your own opinion on",
    "small talk handled by your quick voice",  # what was said since her last turn, without her
    "handled as a quick command without you",
    "you said your last reply again, word for word",
    "your previous answer came from",
    "the user is on a call in ",  # a call app on the mic: only don't remember what may have been for the call
    "for this answer: ",  # how to word it, from her experiments on herself or a habit she kept (futures.habit_rules)
)
_LABEL_WORDS = 5

# Risky or complex by policy: Claude's, whatever the local model would say. Sending, writing, deleting, buying, the
# screen, files, several steps, anything to look up, and anything about the user's own life or her own state that
# only a tool can tell (health, school, to-dos, email, music accounts, her builds): it has no tool for those, so it
# could only guess (it once read out a resting heart rate for "my heart rate right now").
_CLAUDE_ONLY = re.compile(
    r"\b(?:and then|after that|screen|click|window|tab|open|close|type|email|emails|mail|inbox|send|text|message|"
    r"search|google|look up|research|write|draft|remind|reminder|calendar|schedule|book|file|files|folder|code|"
    # Her own work in any tense: "Is anything building right now?" went to the local brain and got a guess.
    r"build|builds|building|built|fix|fixes|fixed|fixing|install|installs|installed|installing|update|updates|updated|"
    r"updating|download|downloads|downloading|improvements?|uninstall|delete|remove|buy|order|pay|purchase|password|"
    r"news|weather|"
    r"summari[sz]e|compare|play|remember|forget|teach|tutor|quiz|homework|heart|heart rate|vitals|garmin|health|"
    r"sleep|steps|stress|body battery|canvas|grade|grades|assignment|assignments|due|deadline|deadlines|class|"
    r"classes|midterm|exam|ticktick|task|tasks|to-?dos?|spotify|playlist|discord|datacamp|data camp|arden|meridian|"
    r"leads?|status)\b"
)
# It must call a tool for these: a reply alone would only say it did it.
COMMAND = re.compile(
    r"\b(?:timers?|volume|louder|quieter|softer|mute|unmute|pause|resume|unpause|skip|"
    r"next (?:song|track)|previous (?:song|track)|last (?:song|track))\b"
)
# Tools that only look, never change anything: the local model may always call these, besides ``route_tools``.
# find_on_screen reads the screen's labels (no screenshot), so the user still approves it as for any brain.
READ_TOOLS = frozenset({"find_on_screen", "list_running_apps", "get_current_time"})
# Tools that act on the screen or the music: the local model may call these only while Claude is away (in "route" mode
# Claude is one step away, and the screen is its). Each goes through the same executor as Claude's own calls, so its
# risk check, the user's approvals and "submit without asking" apply exactly as they do for Claude. One per turn:
# anything more takes several steps, and that's Claude's.
ACT_TOOLS = frozenset({"click", "type_text", "press_keys", "scroll", "resume_screen_task", "media_control"})
# A reply saying it changed something: only true when a tool that acts ran ("I've paused it", "Done", "Sent").
_ACTED = (
    r"\b(?:i'?ve|i have|i'?m|i am|i'?ll|i will|i)\s+(?:just\s+|now\s+|already\s+|gone\s+ahead\s+and\s+)?"
    r"(?:start(?:ed|ing)|open(?:ed|ing)|clos(?:ed|ing)|launch(?:ed|ing)|sen[dt]|sending|set(?:ting)?|turn(?:ed|ing)|"
    r"paus(?:ed|ing)|click(?:ed|ing)|typ(?:ed|ing)|delet(?:ed|ing)|creat(?:ed|ing)|sav(?:ed|ing)|scroll(?:ed|ing)|"
    r"mut(?:ed|ing)|book(?:ed|ing)|schedul(?:ed|ing)|install(?:ed|ing)|skipp(?:ed|ing)|switch(?:ed|ing)|"
    r"remind(?:ed)?|play(?:ed|ing) (?:it|that|the|your|some))\b"
    # "Done.", "Sure, on it.", "Okay, reading the PDFs now.", "Will do."
    r"|^\W*(?:(?:sure|okay|ok|alright|yep|yes|got it|gotcha)\W+)*(?:done|all set|on it|starting|opening|launching|"
    r"paused|sent|skipped|reading|working on|resuming|carrying on|picking it)\b|\bwill do\b"
    # a task's state: "I'm back on the DataCamp", "it's running again", "picked it back up"
    r"|\bback on\b|\b(?:running|going|carrying on) again\b|\bback up\b|\b(?:resumed|unpaused)\b"
)
ACTED = re.compile(_ACTED, re.I)
# Any line about doing something, also a promise or something in progress: a routed turn has no tool that acts, so
# there any such line is a promise nothing keeps ("I'll start reading the PDFs now", "I'm working on the DataCamp"):
# the turn is Claude's instead. After only tools that look (the time, the apps open), "I checked" is true: ``ACTED``.
CLAIMED = re.compile(
    _ACTED
    # a promise: "I'll stop the reader", "let me check", "I'm going to start"; not "let me know" or "I'll be here"
    + r"|\b(?:i'?ll|i will|i'?m going to|i am going to|i'?m gonna|let me|lemme)\s+(?:just\s+|now\s+|go\s+ahead\s+"
    r"and\s+)?(?!(?:know|be|think|guess|say|tell you|explain|put it|ask)\b)[a-z]+"
    # done already: "I've saved it", "I just added it", "I already told them"
    r"|\b(?:i'?ve|i have|i just|i already|i)\s+(?:just\s+|already\s+|now\s+)?(?:added|asked|built|cancel+ed|"
    r"changed|checked|copied|dropped|emailed|fixed|found|logged|looked|moved|noted|picked|posted|read|recorded|"
    r"removed|resumed|restarted|searched|stopped|submitted|updated|wrote|written)\b"
    # in progress: "I'm working on the DataCamp", "I'm reading the PDFs"
    r"|\b(?:i'?m|i am)\s+(?:now\s+|still\s+|already\s+)?(?:working|reading|checking|looking|building|fixing|"
    r"writing|searching|running|resuming|submitting|doing (?:it|that|the))\b"
    # restarting, rebooting, shutting down: never true without a tool ("You got it, restarting now." faked a restart
    # in local only, 2026-10-26)
    r"|\b(?:i'?m|i am)\s+(?:now\s+|just\s+)?(?:restarting|rebooting|shutting (?:myself )?down)\b"
    r"|^\W*(?:(?:sure|okay|ok|alright|yep|yes|you got it|got it|gotcha)\W+)*(?:restarting|rebooting|shutting down)(?:\s+now)?\W*$"
    r"|\b(?:restarting|rebooting) (?:now|myself)\b"
    # a button it says it pressed, or a switch it says it made: "I hit pause on that video and switched you back"
    # (said as done by her: not "you can hit pause", "switched it up")
    r"|\bi\s+(?:just\s+|already\s+)?(?:hit|pressed|clicked|tapped)\s+(?:pause|play|skip|stop|mute|next)\b"
    r"|\b(?:i|and)\s+(?:just\s+|already\s+)?switched\s+(?:you|it|that|things)\s+(?:back|over|to)\b",
    re.I,
)
# A reply about her own updates (a build, a fix, whether or when it installs): the local model can't check any of it,
# so it would only be guessing ("it'll install automatically", when it was started not to). Claude answers those.
UNCHECKED = re.compile(r"\b(?:(?:re)?install(?:s|ed|ing|ation)?|builds?|building|improvements?)\b", re.I)


# What only makes sense against the conversation, said on its own or nearly: a yes or no to something she asked,
# "continue", "do it", "I said...", "what?". The local model once took "yes" for "hold off" and "continue" for
# play the music, so these are Claude's, which has the whole conversation (and the screen task it may be about).
_BARE = frozenset(
    "yes yeah yep yup ya yea sure ok okay alright aight fine no nope nah not continue carry on keep going go ahead do "
    "it that this one please again more next resume proceed right correct exactly true wrong wait what huh why really "
    "and so then also too same instead actually definitely absolutely totally course of mm hmm uh um oh sounds good "
    "great perfect".split()
)
# Words that point back at what was said ("do that", "what about him", "I meant the other one", "did you finish?").
# Not every "it" or "that": "how's it going" and "is that right" stand on their own.
_REFERS = re.compile(
    r"\b(?:(?:do|did|try|fix|send|open|finish|change|undo|redo|use|check|keep|stop) (?:it|that|this|those|them)|"
    r"those|these|him|his|she|he|instead|else|same one|the other|another one|earlier|before|previous|the rest|"
    r"i meant|i mean|i said|you said|didn'?t|did you|have you|were you|are you (?:still|done|finished|working|doing)|"
    r"what about|how about|why not|how come|the one|one more|again)\b"
)
_WITH_ACTION = re.compile(r"^(?:yes|yeah|yep|sure|ok|okay|no|nope|go|please|continue|keep|carry)\b")
_FOLLOW_UP_WORDS = 4  # this many words or fewer, all from ``_BARE``: a bare reply to the conversation
# Her line asked them something: their next words are its answer ("Want me to build it?" "Sure.").
_ASKED = re.compile(
    r"\?\s*$|\?[\s\"')\]]|\b(?:want me to|should i|shall i|would you like|do you want|say (?:yes|the word|continue|"
    r"carry on)|let me know if)\b",
    re.I,
)
# Questions that only keep the talk going ("What's up?", "How about you?"): what follows isn't an answer to anything.
_IDLE_QUESTION = re.compile(r"\b(?:what'?s up|how about you|and you|what'?s on your mind|how are you)\W*$", re.I)


def asks_something(line: str) -> bool:
    """Whether her ``line`` asked the user something they'd answer (not "What's up?")."""
    return bool(_ASKED.search(line)) and not _IDLE_QUESTION.search(line)


def follows_up(said: str, lines: Sequence[tuple[str, str]] = ()) -> str | None:
    """Why ``said`` (a turn's core) hangs on the conversation (``lines``: its latest lines as (role, text), oldest
    first, without this turn), or None: "bare" (a yes, a "continue", a "what?"), "answer" (her last line asked
    something), "refers" (it points back at what was said). Those are Claude's."""
    words = re.findall(r"[a-z']+", said.casefold())
    if not words:
        return None
    if len(words) <= _FOLLOW_UP_WORDS and all(w.strip("'") in _BARE for w in words):
        return "bare"
    if _WITH_ACTION.match(" ".join(words)) and len(words) <= _FOLLOW_UP_WORDS + 2:
        return "bare"  # "yes do it now", "no not that one"
    if not lines:
        return None
    last = next((text for role, text in reversed(lines) if role == "assistant"), "")
    if last and asks_something(last):
        return "answer"
    if _REFERS.search(" ".join(words)):
        return "refers"
    return None


def delegate_odds(message: dict[str, Any]) -> float | None:
    """How likely the local model was to hand the turn over, from its first token's alternatives (its logprobs): the
    odds of its first word being the start of ``DELEGATE``. None when no logprobs came back. ``1 - this`` is how sure
    it was that it should answer itself."""
    tokens = (message.get("logprobs") or {}).get("content") or ()
    for token in tokens:
        chosen = str(token.get("token", "")).strip()
        if not chosen:
            continue  # a newline or space before its first word
        odds = 0.0
        seen = False
        for alt in token.get("top_logprobs") or ():
            text = str(alt.get("token", "")).strip()
            try:
                p = math.exp(float(alt["logprob"]))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            seen = True
            if len(text) >= 2 and DELEGATE.startswith(text):
                odds += p
        if not seen:  # no alternatives: the token it chose, as certain
            return 1.0 if len(chosen) >= 2 and DELEGATE.startswith(chosen) else 0.0
        return min(odds, 1.0)
    return None


# Her notes, said back: what's in the bracket before their words is context and never spoken ("you already said...",
# "from their voice (not their words)...", the time line). The local model once read the whole bracket out loud.
_NOTE_ECHO = re.compile(
    r"you already said|skip any acknowledg|from their voice|not their words|a guess: let it|possibly related, looked up|"
    r"looked up automatically|texted from their phone|typed in your chat window|since your last answer|"
    r"handled as a quick command|your local brain|\b(?:EDT|EST|CDT|CST|PDT|PST|UTC)\s*[;\]]",
    re.I,
)
_ECHO_CHARS = 40  # a run of this many characters of a note, said back word for word


def echoes_context(text: str, notes: Iterable[str] = ()) -> bool:
    """Whether a reply repeats the turn's own context (its notes, the time line) instead of answering."""
    if _NOTE_ECHO.search(text):
        return True
    flat = " ".join(text.casefold().split())
    for note in notes:
        words = " ".join(note.casefold().split())
        if len(words) < _ECHO_CHARS:
            continue
        starts = [*range(0, len(words) - _ECHO_CHARS + 1, _ECHO_CHARS // 5), len(words) - _ECHO_CHARS]
        if any(words[i : i + _ECHO_CHARS] in flat for i in starts):
            return True
    return False


_RULES = (
    "Answer it yourself only when you're sure it's simple and stands on its own: a greeting, how you are, thanks, "
    "a joke or light chit-chat, a well-known fact you're certain of, simple arithmetic, or one simple action or "
    f"check with one of your tools. Everything else is your cloud brain's: reply with only one line, {DELEGATE}: and "
    f'a few words on why (for example "{DELEGATE}: needs a web search"), and nothing else; it answers instead and they '
    "never hear the handoff. That covers anything you'd have to look up or check (their health, school, grades, "
    "to-dos, email, calendar, music, money, people in their life, your own updates, builds or installs, or what "
    "you're working on), the screen, files, "
    "writing or sending, remembering or correcting something, teaching, advice or real reasoning, several steps, a "
    "reply that only makes sense with what was said before (yes, no, sure, continue, do it, what about...), and "
    f"anything you're unsure about. When in doubt, {DELEGATE}. "
    "Never say you did, started or will do something without calling its tool, or that you're doing it: no \"I'll "
    'start", "I\'m on it", "I\'ve saved it". Never mention Claude, handing over or your local brain. '
    "The bracketed line before their words (the time, and notes) is context for you, never something to say or repeat."
)

_EXAMPLES = (
    ("hey, how's it going?", "Pretty good. What's up?"),
    ("what's 12 times 12", "144."),
    ("thanks", "Anytime."),
    ("what's the capital of France", "Paris."),
    ("tell me a joke", "I'd tell you a UDP joke, but you might not get it."),
    ("yes", f"{DELEGATE}: answers something said before"),
    ("continue", f"{DELEGATE}: carries on with something"),
    ("sure, go ahead", f"{DELEGATE}: answers something said before"),
    ("what's my heart rate right now", f"{DELEGATE}: needs their watch data"),
    ("did you finish that?", f"{DELEGATE}: about what's been happening"),
    ("what do I have due this week", f"{DELEGATE}: needs Canvas"),
    ("can you read those PDFs for me", f"{DELEGATE}: needs files and several steps"),
    ("no, I meant the other one", f"{DELEGATE}: a correction"),
    ("should I take the job offer", f"{DELEGATE}: needs real reasoning"),
)


def instructions(tools: Iterable[str]) -> str:
    """What the local model is told about answering or handing over, after the rest of her prompt (the shadow, which
    measures what routing would do with her whole prompt)."""
    names = ", ".join(dict.fromkeys([*tools, *sorted(READ_TOOLS)])) or "none"
    return (
        "\n\n# On your local brain\n"
        "You're running on a small local model, and your cloud brain is one step away. You hear each turn first, "
        f"spoken or texted. Your tools: {names}. {_RULES}"
    )


def focused_prompt(name: str, user: str, persona: str, voice_style: str | None, tools: Iterable[str]) -> str:
    """The local model's whole system prompt when it answers turns: a small focused job, not her full prompt and
    every tool (~42k tokens of it confused a small model). Who she is in a few lines, when to answer and when to hand
    over, and examples of both. The same every turn, so the server keeps it cached."""
    names = ", ".join(dict.fromkeys(tools)) or "none"
    who = user or "the user"
    examples = "\n".join(f"{who}: {said}\nYou: {reply}" for said, reply in _EXAMPLES)
    parts = [
        f"You are {name}, {who}'s personal assistant on their Windows PC. You talk out loud, so reply in one or two "
        "short spoken sentences of plain text: no markdown, lists or links.",
        persona.strip(),
        voice_style.strip() if voice_style else "",
        "# Your job right now\nYou're the quick local brain: you hear each turn first, and your cloud brain, which "
        "has the whole conversation, every tool and much better judgment, is one step away. Your tools: "
        f"{names}. {_RULES}",
        f"# Examples\n{examples}",
    ]
    return "\n\n".join(p for p in parts if p)


def delegated(text: str) -> bool:
    """It handed the turn to Claude."""
    return bool(_DELEGATED.match(text))


def delegation_reason(text: str) -> str | None:
    """Why it handed the turn to Claude ("" when it didn't say), or None when it didn't hand it over."""
    found = _DELEGATED.match(text)
    if found is None:
        return None
    return " ".join(found.group(1).replace("*", " ").split()).strip(" .\"'")[:_WHY_CHARS]


def required_note(notes: Iterable[str]) -> str | None:
    """The first note that makes a turn Claude's (its first few words, for the log), or None when every note is only
    context the local model may hear. A text from the phone is only context too, but the Discord bridge joins its
    other notes (files sent, a voice message heard) after it into one, and those stay Claude's."""
    for note in notes:
        if note.startswith(TEXTED_NOTE):
            note = note.removeprefix(TEXTED_NOTE).removeprefix("; ")
            if not note:
                continue
        if not note.startswith(_CONTEXT_NOTES):
            return " ".join(re.split(r"[;:(\"]", note, maxsplit=1)[0].split()[:_LABEL_WORDS])[:40] or "note"
    return None


def policy(text: str) -> str | None:
    """Why this turn is Claude's whatever the local model would say (the risky or complex word), or None."""
    found = _CLAUDE_ONLY.search(text.casefold())
    return found.group(0) if found else None
