"""The user's own words as the yes: which tool calls they can approve, and which words count.

The user chose that when they themselves clearly ask Octavia to look at their screen ("what's on my screen?"),
that request is the approval; she only asks when she wants to look on her own initiative (unless their settings
let her look on her own: ``permissions.tools: {look_at_screen: auto}``, see ``Decision.AUTO``). The same goes for
installing her ready updates: "install everything that's ready and restart" is the yes, and the question ("install
... and restart for a few seconds?") only comes when the words don't plainly ask for that very install. Skipping a question
loosens a permission, so the decision lives here, in protected code like the permission policy and the executor:
a self-improvement build can't add a tool to the list or change which words count, not even through a file Python
would import in this one's place (``evolve.policy`` protects those too), and a tool can't vouch for itself. The
executor asks ``matcher_for`` and nothing else. Code that rebinds these names while it runs is beyond what any
file check sees; that is for the user's review of a build.

The matcher is deliberately simple and conservative: an actual request to look at, find, read or point at
something, tied to the screen or to a control on it, and nothing that says no or takes the request back. When in
doubt it says no, and she asks as usual. It is only ever given what the user said or typed in the running turn,
or, when that was a plain yes, the question of hers it answered (``questions_in``, ``Assistant.offered``), never a
tool's output (``Assistant.requested``).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from octavia.tools.base import Tool

_SCREEN_WORD = r"(?:screens?|monitors?|displays?|desktops?)"
_NOT_ABOUT_LOOKING = (
    r"(?:time|savers?|readers?|resolution|brightness|apps?|application|version|client|computer|pc|folders?|files?|"
    r"settings?|drivers?|cables?|ports?|adapters?)"
)
_DETERMINER = r"(?:my|the|this|that|our|your|both|all|either|each|whole|entire|other)"
# "my screen", "the left monitor", "both displays", "my laptop's screen"; not "screen time", "desktop app",
# "my desktop folder", "display settings".
_SCREEN = rf"\b{_DETERMINER}\b(?:\s+[\w'-]+){{0,2}}?\s+{_SCREEN_WORD}\b(?!\s+{_NOT_ABOUT_LOOKING}\b)"
# Where on the screen something is: "on my screen", "across both monitors".
_ON_SCREEN = rf"\b(?:on|in|at|across)\s+{_SCREEN}"
# Controls she can find and point at. Only words that mostly mean something on a screen: not "bar", "box",
# "window" or "switch" on their own, which are as often a pub, a parcel or a light.
_CONTROL = (
    r"(?:buttons?|icons?|settings?|menus?|tabs?|links?|toggles?|sliders?|cursor|dialogs?|pop-?ups?|drop-?downs?|"
    r"check\s?box(?:es)?|(?:text|search|input|password)\s?(?:box(?:es)?|fields?)|"
    r"(?:tool|task|address|search|menu|scroll|status|title|side|nav|navigation)\s?bars?)"
)
_LOOK = (
    r"(?:look|looking|see|seeing|check|checking|read|reading|scan|scanning|glance|glancing|peek|peeking|view|viewing)"
)
# A verb, not a noun after "the", "a", "my": "the point to remember", "delete the screenshot my phone took".
_NOT_A_NOUN = r"(?<!\bthe\s)(?<!\ba\s)(?<!\bmy\s)(?<!\byour\s)(?<!\bthis\s)(?<!\bthat\s)"
_POINT = rf"{_NOT_A_NOUN}\bpoint(?:ing)?"
_SCREENSHOT = r"screen\s*(?:shot|grab|cap|capture)s?"
_ASKS = re.compile(
    "|".join(
        (
            # "look at my screen", "check my screen for errors", "can you see my display", "take a look at both
            # monitors"; not "why does my screen flicker", "copy the report to my desktop", "turn off the display"
            rf"\b{_LOOK}\b(?:\s+(?:at|over|through|into))?\s+{_SCREEN}",
            # "find the apple on my screen", "read the error on my monitor", "what do you see on my screen", "point
            # at the clock on my screen"; not "check the price of my monitor"
            rf"\b(?:{_LOOK}|find|finding|spot|spotting|search|searching)\b(?:\s+[\w'-]+){{0,5}}?\s+{_ON_SCREEN}",
            # (with a word between: "a stuck point on my screen" is a noun)
            rf"{_POINT}\b(?:\s+[\w'-]+){{1,5}}?\s+{_ON_SCREEN}",
            # "what's on my screen", "what is this popup on my monitor"; not "what files are on my desktop"
            rf"\bwhat(?:'s|s|\s+is|\s+are)\b(?:\s+[\w'-]+){{0,3}}?\s+{_ON_SCREEN}",
            # "is there anything weird on my desktop", "is something wrong on my screen"
            rf"\b(?:is|are)(?:\s+there)?\s+(?:anything|something)\b(?:\s+[\w'-]+){{0,3}}?\s+{_ON_SCREEN}",
            # "I don't know where the save button is on my screen", "where on my screen is the clock"
            rf"\bwhere\b(?:\s+[\w'-]+){{0,5}}?\s+{_ON_SCREEN}",
            # "what does the left monitor say", "read what my screen says"
            rf"\bwhat\s+(?:(?:does|do)\s+)?{_SCREEN}\s+(?:says?|shows?)\b",
            r"\bon[\s-]?screen\b",
            # Asking for a screenshot, not just saying the word: "take a screenshot", "can you snap a quick screenshot
            # of my screen", "screenshot", "octavia, screenshot this"; not "turn off screenshots", "the screenshot you
            # took was blurry", "grab the screenshot I took yesterday", "make a screenshot folder"
            rf"\b(?:take|grab|get|snap|capture|make|send\s+me|do)\s+(?:a|an|another|one)\s+(?:(?:quick|new|fresh|"
            rf"full|full-screen|fullscreen)\s+)?{_SCREENSHOT}\b(?!\s+(?:folders?|files?|tools?|apps?|shortcuts?|keys?))",
            r"^(?:[\w'-]+,\s*)?(?:please\s+)?screen\s*shot(?=\s*[.!]*\s*$|,?\s+(?:please|now|and|of|for)\b)",
            rf"{_NOT_A_NOUN}\bscreen\s*shot\s+(?:it|this|that|my|the)\b",
            r"\bwhat\s+(?:i'm|im|i\s+am|am\s+i)\s+(?:looking\s+at|seeing|viewing)\b",
            # "point it out for me"; "point at the save button", "can you point to the settings icon", "show me where
            # the download button is"; not "point me to a good tutorial", "point out the flaws in my plan", "show me
            # where the nearest pizza place is"
            rf"{_POINT}\s+(?:it|them|that|this|those)\s+out\b",
            rf"{_POINT}(?:\s+me)?\s+(?:at|to|out)\s+(?:the|this|that|my|your)\b(?:\s+[\w'-]+){{0,3}}?\s+{_CONTROL}\b",
            rf"\bshow\s+me\s+where\b(?:\s+[\w'-]+){{0,4}}?\s+{_CONTROL}\b",
        )
    )
)
_NEGATOR = (
    r"(?:\w+n't|dont|doesnt|didnt|cant|cannot|wont|wouldnt|shouldnt|isnt|do\s+not|can\s+not|never|stop|quit|not|no|"
    r"without|avoid|refrain)"
)
_LOOKING = (
    r"(?:look|looks|looking|see|seeing|peek|peeking|glance|glancing|check|checking|read|reading|take|taking|grab|"
    r"grabbing|capture|capturing|screen\w*|monitors?|displays?|desktops?|watch|watching|snoop|snooping|spy|spying|"
    r"point|pointing|find|finding|search|searching|scan|scanning|record|recording|share|sharing|use|using)"
)
_REFUSES = re.compile(
    "|".join(
        (
            # A negator, at most three words (never across a comma or an "and") before a looking word: "don't
            # look", "never take a screenshot", "you do not need to see my screen", "stop sharing my screen".
            rf"\b{_NEGATOR}\b(?:\s+(?!(?:and|but|then|so|or|because|if|when)\b)[\w']+){{0,3}}?\s+{_LOOKING}\b",
            rf"\b(?:leave|stay\s+(?:off|out\s+of|away\s+from)|keep\s+(?:off|out\s+of|away\s+from)|get\s+off|"
            rf"hands\s+off)\b(?:\s+[\w']+){{0,2}}?\s+{_SCREEN_WORD}\b",
            # A question about her own looking, often an objection to it: "why are you looking at my screen", "did
            # you just take a screenshot", "who told you to look". Not "can you look", "what do you see on my
            # screen" or "I was wondering if you could look", which ask.
            r"\b(?:why|who|did|didn't|didnt|have|has|were|was|are)\b(?:\s+[\w']+){0,3}?\s+you\b"
            r"(?:\s+(?!(?:can|could|would|will|might|may|please|able)\b)[\w']+){0,3}?\s+"
            r"(?:look|looked|looking|see|saw|seen|seeing|take|took|taken|taking|screen\w*|peek\w*|snoop\w*|watch\w*|"
            r"read|reading)\b",
            # A condition, not a go-ahead: "ask me before you look", "only look at my screen when I say so", "not
            # until I tell you", "look without asking".
            r"\bask\s+(?:me\s+)?(?:first|before)\b|\bonly\b(?:\s+[\w']+){0,6}?\s+(?:when|if|after)\s+i\b",
            r"\b(?:until|unless)\s+i\b|\bwithout\s+(?:asking|permission|my\s+(?:ok|okay|permission))\b",
            # Whether she was allowed to, which questions it: "who said you could look at my screen", "did I say
            # you could", "what makes you think you can", "since when", "you could have asked", "were you able to".
            r"\b(?:say|said|says|told|tell|think|thinks|thought)\s+(?:(?:that|you)\s+)?you\s+(?:can|could|may|might)\b",
            r"\bsince\s+when\b|\b(?:could|should)(?:\s+have|'ve)\s+asked\b|\b(?:were|was)\s+you\s+able\b",
            # Asking how to do it themselves: "how do I take a screenshot on windows", "how to take a screenshot",
            # "what's the shortcut to take a screenshot".
            r"\bhow\s+(?:do|does|can|could|would|should)\s+(?:i|we|you|one)\b",
            r"\b(?:how|shortcut|hotkey|key|keys|way|button)\s+(?:to|for)\s+(?:take|taking|grab|snap|capture|make|get)\b",
            # Their own plans, not a request: "let me take a screenshot", "I'll grab a screenshot", "remind me to
            # take a screenshot later".
            r"\b(?:let\s+me|i'll|ill|i\s+will|i'm\s+going\s+to|im\s+going\s+to|i\s+(?:want|need|have)\s+to|"
            r"remind\s+me\s+to)\s+(?:[\w']+\s+){0,2}?(?:take|grab|snap|capture|make|get)\b",
        )
    )
)
# Taking it back after asking: "take a screenshot. actually, never mind", "look at my screen. no wait, don't do
# that", "can you check my screen? no, forget it", "take a screenshot. cancel.", "take a screenshot? nope".
# Anywhere in what they said: she asks once, the safe side. A plain "stop" or "no" only counts at the very end:
# "Stop. Look at my screen." and "no, look at my other monitor" ask. Not "hold on", which is only a pause.
_RETRACTS = re.compile(
    r"\b(?:never\s*mind|nvm|forget\s+(?:it|that|about\s+it)|scratch\s+that|cancel\s+that|don't\s+bother|dont\s+bother|"
    r"not\s+now|not\s+yet|no\s+wait|wait[.,!]?\s+no|actually,?\s+(?:no|don't|dont)|on\s+second\s+thought|"
    r"(?:just\s+)?kidding|jk|nah|nope|no\s+thanks?|no\s+thank\s+you|hold\s+off)\b"
    # Not yet, then: "look at my screen when I'm done", "in a minute", "later".
    r"|\b(?:in\s+a\s+(?:minute|min|sec|second|bit|moment|few)|later|(?:when|once|after)\s+i(?:'m|m|\s+am)?\s+"
    r"(?:done|finished|ready|back))\b"
    # "cancel", but not the cancel button she's asked to find
    r"|\bcancel\b(?!\s+(?:buttons?|links?|icons?|options?|keys?)\b)"
    r"|\b(?:don't|dont|do\s+not)(?:\s+do)?(?:\s+(?:that|it))?\s*[.!]?\s*$"
    r"|\b(?:stop|no)\s*[.!]*\s*$"
)
# Typed text and some transcripts use typographic apostrophes (a curly "don't"): the patterns expect plain ones.
_APOSTROPHES = str.maketrans({"\N{RIGHT SINGLE QUOTATION MARK}": "'", "\N{LEFT SINGLE QUOTATION MARK}": "'", "`": "'"})


def asks_to_look(text: str) -> bool:
    """Whether the user's own words plainly ask to look at, find, point at or read something on their screen.

    True for "what's on my screen", "take a screenshot", "point at the save button", "show me where the settings
    icon is"; False for anything that says no to it ("don't look at my screen", "never take a screenshot"), objects
    to or asks about her looking ("why are you looking at my screen?"), sets a condition ("ask me before you look"),
    takes it back ("take a screenshot. actually, never mind", "take a screenshot. cancel."), only mentions a screen
    or a screenshot ("why does my screen keep flickering", "turn off screenshots") or asks for something else
    ("point me to a good tutorial", "how do I take a screenshot").
    """
    said = " ".join(text.lower().translate(_APOSTROPHES).split())
    return (
        bool(said)
        and _ASKS.search(said) is not None
        and _REFUSES.search(said) is None
        and _RETRACTS.search(said) is None
    )


def refuses_look(text: str) -> bool:
    """Whether the user's words tell her not to look at their screen, or object to it ("don't look at my screen"). Words
    that only mention a screen ("what does this mean on my screen?") don't."""
    return _REFUSES.search(" ".join(text.lower().translate(_APOSTROPHES).split())) is not None


# ---- installing her updates ----

_INSTALL = r"\binstall(?:ing)?\b"  # not "uninstall" or "reinstall"
# What gets installed, a few words after the verb: "install it", "install everything that's ready", "install the
# update", "install number 12", "go ahead and install them all".
_INSTALL_ASKS = re.compile(
    rf"{_INSTALL}(?:\s+[\w'#-]+){{0,4}}?\s+(?:it|them|that|this|those|these|both|everything|all|every|each|"
    r"(?:the|my|your|new|ready)\s+(?:[\w'-]+\s+){0,2}?(?:updates?|improvements?|fix(?:es)?|ones?|builds?)|"
    r"updates?|improvements?|fix(?:es)?|what(?:'s|s|\s+is|\s+you)|number|#\d+|\d+)\b"
)
# "Install" as a noun, for the install being talked about (one update or the batch): "go ahead and do the install",
# "run the installs", "go ahead with the installation".
_INSTALL_NOUN = re.compile(
    r"\b(?:do|run|start|finish|go\s+ahead\s+with|proceed\s+with|continue\s+with)\s+(?:the|that|this|those|these|"
    r"your|my)\s+(?:installs?|installations?)\b"
)
_INSTALL_REFUSES = re.compile(
    "|".join(
        (
            # "don't install", "do not install that one", "never install anything"
            rf"\b{_NEGATOR}\b(?:\s+(?!(?:and|but|then|so|or|because|if|when)\b)[\w']+){{0,3}}?\s+install",
            # Asking about it, not for it: "should I install it", "is it safe to install", "what would installing
            # do", "did you install it", "why did you install that", "how do I install updates"
            r"\b(?:should|shall|would|could)\s+(?:i|we)\b|\bis\s+it\s+(?:safe|ok|okay|worth|a\s+good\s+idea)\b",
            r"\bwhat\s+(?:would|does|do|will|happens|if|did)\b|\bwhether\b",
            r"\b(?:why|who|did|didn't|didnt|have|has|were|was)\b(?:\s+[\w']+){0,3}?\s+you\b(?:\s+[\w']+){0,2}?\s+install",
            r"\bhow\s+(?:do|does|can|could|would|should)\s+(?:i|we|you|one)\b",
            # A condition, not a go-ahead: "install it once it's built", "only install when I say", "ask me first"
            r"\b(?:when|once|after|as\s+soon\s+as)\s+(?:it'?s|it\s+is|they'?re|they\s+are|it|they|you'?re|you)\b",
            r"\bask\s+(?:me\s+)?(?:first|before)\b|\bonly\b(?:\s+[\w']+){0,6}?\s+(?:when|if|after)\s+i\b",
            r"\b(?:until|unless)\s+i\b|\b(?:tonight|tomorrow|this\s+evening|in\s+the\s+morning)\b",
            # Their own plan: "let me install it", "I'll install it myself", "remind me to install it"
            r"\b(?:let\s+me|i'll|ill|i\s+will|i'm\s+going\s+to|im\s+going\s+to|remind\s+me\s+to)\s+(?:[\w']+\s+){0,2}?"
            r"install",
        )
    )
)
# Words that ask for more than one: installing several (or all that are ready) needs one of them.
_SEVERAL = re.compile(
    r"\b(?:them|those|these|both|everything|all|every|each|updates|improvements|fixes|ones|builds|"
    r"(?:that|what|which|whatever)(?:'s|s|\s+is|\s+are|'re)\s+ready|they(?:'re|\s+are)\s+ready)\b"
)
_NUMBER_WORDS = {
    word: i
    for i, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen twenty".split()
    )
}
# Updates named by their number: "number 12", "#12", "improvement 12", "update number three".
_NAMED = re.compile(
    r"(?:#|\b(?:number|no\.?|improvement|update|fix|build)\s+(?:number\s+)?#?)(\d+|" + "|".join(_NUMBER_WORDS) + r")\b"
)


def asks_to_install(text: str, args: Mapping[str, object] | None = None) -> bool:
    """Whether the user's own words plainly ask to install her ready updates, and ask for this very call
    (``args``: the install's arguments, ``improvement_ids`` and ``all_ready``).

    True for "go ahead and batch install everything that's ready and then restart", "install it", "yes, install
    number 12", "go ahead and do the install" (the install being talked about, one update or all of them); False for anything that says no ("don't install it"), asks about it ("should I install it?", "what
    would installing do?"), sets a condition ("install it once it's built", "ask me first"), takes it back ("install
    it. actually, never mind"), or doesn't fit the call: "install it" for several updates or for all that are ready,
    "install number 3" for another one.
    """
    said = " ".join(text.lower().translate(_APOSTROPHES).split())
    noun = _INSTALL_NOUN.search(said) is not None
    if not said or (_INSTALL_ASKS.search(said) is None and not noun):
        return False
    if _INSTALL_REFUSES.search(said) is not None or _RETRACTS.search(said) is not None:
        return False
    call = args or {}
    given = call.get("improvement_ids") or []
    ids = {i for i in given if isinstance(i, int)} if isinstance(given, (list, tuple, set)) else set()
    all_ready = call.get("all_ready") is True
    named = {int(m) if m.isdigit() else _NUMBER_WORDS[m] for m in _NAMED.findall(said)}
    if named:  # the ones they named, and only those
        return not all_ready and bool(ids) and ids <= named
    if all_ready or len(ids) > 1:
        return noun or _SEVERAL.search(said) is not None
    return len(ids) == 1


# ---- building an improvement ----
#
# A build only uses some Claude usage and changes nothing until it's installed (which is asked on its own), so the
# user's plain "go ahead and build it" is the yes.

_BUILD = r"(?:re)?build"
_BUILD_ASKS = re.compile(
    "|".join(
        (
            # a request: "go ahead and build", "please build", "she needs to build it", "build that fix"
            r"(?:^|[.,!?;]\s*|\b(?:and|then|please|just|to|can\s+you|could\s+you|you\s+can|go\s+ahead\s+and|"
            rf"need\s+to|needs\s+to)\s+){_BUILD}\b",
            rf"\b{_BUILD}\s+(?:it|that|this|them|those|(?:the|that|this|my)\s+(?:fix|update|improvement|change)|#?\d+|"
            r"number\s+\w+)\b",
            r"\b(?:start|kick\s+off|fire\s+off)\s+(?:the\s+)?build(?:ing)?\b",
        )
    )
)
_BUILD_REFUSES = re.compile(
    "|".join(
        (
            rf"\b{_NEGATOR}\b(?:\s+(?!(?:and|but|then|so|or|because|if|when)\b)[\w']+){{0,3}}?\s+(?:re)?build",
            r"\b(?:should|shall|would|could)\s+(?:i|we|you)\b|\bwhat\s+(?:would|does|do|will|happens|if)\b",
            r"\b(?:why|did|have|has|were|was)\b(?:\s+[\w']+){0,3}?\s+you\b(?:\s+[\w']+){0,2}?\s+build",
            r"\bhow\s+(?:do|does|can|could|would|should)\s+(?:i|we|you|one)\b|\bwhether\b",
            r"\bask\s+(?:me\s+)?(?:first|before)\b|\b(?:until|unless)\s+i\b",
            r"\b(?:let\s+me|i'll|i\s+will|remind\s+me\s+to)\s+(?:[\w']+\s+){0,2}?build",
        )
    )
)


def asks_to_build(text: str, args: Mapping[str, object] | None = None) -> bool:
    """Whether the user's own words plainly ask her to build an improvement: "go ahead and build that fix", "build
    it"; not "don't build it", "should you build it?", "ask me first" or a request taken back."""
    said = " ".join(text.lower().translate(_APOSTROPHES).split())
    if not said or _BUILD_ASKS.search(said) is None:
        return False
    return _BUILD_REFUSES.search(said) is None and _RETRACTS.search(said) is None


# ---- restarting herself ----
#
# A restart takes her away for a few seconds and changes nothing else, so "can you do a restart?" is the yes. Only when
# it would cut off nothing: one that stops a build in progress is flagged by the tool (``RestartNow.inspect_risk``),
# and words never cover a flagged call (``_ONLY_FOR``), so that question is still asked.

# "restart", "restart yourself", "restart now", "do a (quick) restart", "go ahead and restart", "reboot".
_RESTART_ASKS = re.compile(
    r"\b(?:restart|reboot|relaunch)(?:\s+(?:yourself|you|octavia|now|right\s+now|again|real\s+quick|quickly|please|"
    r"for\s+me))*\s*(?:$|[.!?,]|\b(?:and|so|then|to|into|with)\b)"
    r"|\b(?:do|run|give\s+(?:yourself|it)|go\s+for)\s+(?:a|another|your|that)\s+(?:quick\s+)?(?:restart|reboot)\b"
)
_RESTART_REFUSES = re.compile(
    "|".join(
        (
            # "don't restart", "no need to restart", "never restart while I'm gaming"
            rf"\b{_NEGATOR}\b(?:\s+(?!(?:and|but|then|so|or|because|if|when)\b)[\w']+){{0,3}}?\s+(?:restart|reboot)",
            r"\bno\s+need\b",
            # Asking about it: "do you need to restart?", "should you restart?", "why did you restart?", "what does a
            # restart do?", "how long does a restart take?"
            r"\b(?:do|does|did|should|shall|must|have|has)\s+(?:you|it|i|we|that)\b(?:\s+[\w']+){0,3}?\s+(?:restart|reboot)",
            r"\b(?:why|what|how|whether)\b(?:\s+[\w']+){0,4}?\s+(?:restart|reboot)",
            # A condition: "restart once the build is done", "restart after this", "restart tonight", "restart later"
            r"\b(?:when|once|after|as\s+soon\s+as|until|unless|before)\b|\b(?:tonight|tomorrow|later|in\s+a\s+(?:bit|while))\b",
            # Another thing restarted: "restart my computer", "restart Spotify", "reboot the PC"
            r"\b(?:restart|reboot|relaunch)\s+(?:my|the|this|that|spotify|discord|chrome|steam|windows|computer|pc)\b",
        )
    )
)


def asks_to_restart(text: str) -> bool:
    """Whether the user's own words plainly ask her to restart herself: "can you do a restart?", "restart yourself",
    "go ahead and restart"; not "don't restart", "do you need to restart?", "restart once it's built", "restart my
    computer" or a request taken back."""
    said = " ".join(text.lower().translate(_APOSTROPHES).split())
    if not said or _RESTART_ASKS.search(said) is None:
        return False
    return _RESTART_REFUSES.search(said) is None and _RETRACTS.search(said) is None


# ---- shutting herself down ----
#
# She goes away until she's started again (at the next login, with autostart), and changes nothing else: "shut down",
# "turn yourself off" is the yes, said to her at the PC. Only then: a shutdown that would stop a build, an install or
# a request from far away (a text, a Discord call: nobody there could start her again) is flagged by the tool
# (``ShutDownSelf.inspect_risk``), and words never cover a flagged call (``_ONLY_FOR``). Never the computer: "shut down
# my computer" isn't this.

_HER = r"(?:yourself|urself|octavia)"
# "shut down", "shut yourself down", "turn yourself off", "power off", "go to sleep", "do a shutdown", "exit yourself".
# A bare "turn off" is asked: off what?
_SHUT_DOWN_ASKS = re.compile(
    rf"\b(?:shut|turn|switch|power)\s+{_HER}\s+(?:down|off)\b"
    rf"|\b(?:shut|power)\s+(?:down|off)(?:\s+{_HER})?(?:\s+(?:now|right\s+now|please|for\s+(?:me|now|the\s+"
    r"(?:night|day))|completely|fully|all\s+the\s+way|already))*\s*(?:$|[.!?,]|\b(?:and|so|then)\b)"
    r"|\b(?:do|run|go\s+for)\s+(?:a|your)\s+(?:full\s+)?shut\s*down\b"
    r"|(?:^|[.!?,]\s*|\b(?:just|please|can\s+you|could\s+you|you\s+can|to|and|octavia)\s+)go\s+to\s+sleep(?:\s+(?:now|"
    r"please|for\s+(?:the\s+night|now)))*\s*(?:$|[.!?,])"
    rf"|\b(?:exit|close|quit)\s+{_HER}\b"
)
_SHUT_DOWN_REFUSES = re.compile(
    "|".join(
        (
            # "don't shut down", "no need to turn off", "never shut yourself down"
            rf"\b{_NEGATOR}\b(?:\s+(?!(?:and|but|then|so|or|because|if|when)\b)[\w']+){{0,3}}?\s+"
            r"(?:shut|turn|switch|power|go\s+to\s+sleep|do\s+a\s+shut)",
            r"\bno\s+need\b",
            # Asking about it: "do you shut down at night?", "can I turn you off?", "why did you shut down?", "how do I
            # shut you down?"
            r"\b(?:do|does|did|should|shall|must|have|has|will|would)\s+(?:you|it|i|we|that)\b(?:\s+[\w']+){0,3}?\s+"
            r"(?:shut|turn|switch|power|go\s+to\s+sleep)",
            r"\b(?:why|what|how|whether|who)\b(?:\s+[\w']+){0,4}?\s+(?:shut|turn|switch|power|go\s+to\s+sleep)",
            # A condition: "shut down once I'm done", "turn off when I leave", "shut down tonight", "in ten minutes"
            r"\b(?:when|once|after|as\s+soon\s+as|until|unless|before|if)\b|\b(?:tonight|tomorrow|later|in\s+a\s+(?:bit|while)|"
            r"in\s+\w+\s+(?:seconds?|minutes?|hours?))\b",
            # Another thing: "shut down my computer", "turn off the PC", "power off Windows", "turn off the lights",
            # "shut down Steam"
            r"\b(?:computer|pc|laptop|desktop|machine|windows|system|monitor|screen|display|lights?|tv|game|steam|"
            r"discord|spotify|chrome|browser|app|program|server)\b",
            r"\b(?:shut|turn|switch|power)\s+(?:down|off)\s+(?:my|the|this|that|his|her|their|our|all|everything)\b",
            # Somebody else going to sleep: "I'm going to sleep", "I want to go to sleep", "time for me to go to sleep"
            r"\b(?:i'?m|i\s+am|i'?ll|i\s+will|i\s+want|i\s+need|i\s+should|i\s+gotta|let\s+me|we|me|he|she|they)\b"
            r"(?:\s+[\w']+){0,3}?\s+(?:go(?:ing)?\s+to\s+sleep|gonna\s+sleep)",
            # Coming back after: "shut down and restart", "shut down, then turn back on", "shut down so I can update"
            # (that's a restart, which she has a tool for)
            r"\b(?:restart|reboot|relaunch|turn\s+(?:yourself\s+)?(?:back\s+)?on|come\s+back|update|reload)\b",
            # Somebody or something else: "tell him to go to sleep", "it's going to shut down", "the store shut down"
            r"\b(?:tell|told|telling|ask|asked|make)\s+(?:him|her|them|it|my|the)\b|\b(?:he|she|it|they|him|them|"
            r"it'?s|that'?s|this|government|store|shop|company|business|place|plant|factory|school|website|site)\b",
        )
    )
)


def asks_to_shut_down(text: str) -> bool:
    """Whether the user's own words plainly ask her to shut herself down: "shut down", "I just want you to shut down",
    "turn yourself off", "go to sleep"; not "shut down my computer", "don't shut down", "do you ever turn off?",
    "shut down when I'm done", "I'm going to sleep" or a request taken back."""
    said = " ".join(text.lower().translate(_APOSTROPHES).split())
    if not said or _SHUT_DOWN_ASKS.search(said) is None:
        return False
    return _SHUT_DOWN_REFUSES.search(said) is None and _RETRACTS.search(said) is None


# ---- doing hands-on work on the screen ----
#
# A screen task looks at the screen and uses the mouse and keyboard beside the conversation. "Look at my screen and go
# ahead and do this" is the yes to starting one: the user asked her to do the work in front of them. What the task
# then does on its own never covers sending, buying, deleting or installing (``_screen_tasks.TaskConfirmer`` refuses
# those, and the task says what's left for the user), so the words only ever start everyday clicks and typing. A
# start its own look flags is asked (``_ONLY_FOR``). Never a quiz, test or exam: a timed one hands itself in.

_DOING = (
    r"(?:do|finish|complete|handle|fill\s+(?:out|in)|work\s+(?:through|on)|go\s+through|get\s+through|knock\s+out|"
    r"take\s+care\s+of|deal\s+with|edit|fix|type\s+(?:out|up|in)|answer|click\s+through)"
)
_WHAT = (
    r"(?:this|that|it|these|those|them|everything|all\s+(?:of\s+)?(?:it|this|that|them)|the\s+rest|"
    r"(?:the|this|that|my|these|those)\s+(?:[\w'-]+\s+){0,3}?[\w'-]+)"
)
_TASK_ASKS = re.compile(
    "|".join(
        (
            # "go ahead and do this", "please finish it", "can you fill this out", "I want you to do the lab"
            rf"\b(?:go\s+ahead\s+and|please|just|can\s+you|could\s+you|would\s+you|will\s+you|i\s+want\s+you\s+"
            rf"to|i\s+need\s+you\s+to|i'd\s+like\s+you\s+to|i\s+would\s+like\s+you\s+to)\s+(?:(?:start|try)\s+(?:to\s+)?)?"
            rf"{_DOING}\s+{_WHAT}\b",
            # starting a sentence or after "and": "Do this for me.", "look at my screen and do this", "finish the lab"
            rf"(?:^|[.,!?;]\s*|\b(?:and|then|now|so)\s+)(?:[\w'-]+,\s*)?{_DOING}\s+{_WHAT}\b",
            # "take over", "do it for me", "you do it", "take it from here"
            r"\btake\s+(?:it\s+)?(?:over|from\s+here)\b|\byou\s+(?:do|finish|handle)\s+(?:it|this|that)\b",
            # "fill this out", "type it up", "knock this out for me"
            r"\b(?:fill|type|knock)\s+(?:this|that|it|them|these|those)\s+(?:out|in|up)\b",
        )
    )
)
_TASK_REFUSES = re.compile(
    "|".join(
        (
            # "don't do it", "do not finish that", "never fill it in"
            rf"\b{_NEGATOR}\b(?:\s+(?!(?:and|but|then|so|or|because|if|when)\b)[\w']+){{0,3}}?\s+(?:{_DOING}|fill|take\s+over)\b",
            # Asking how, or about it: "how do I do this", "what do I do here", "should I do it", "did you do it"
            r"\bhow\s+(?:do|does|can|could|would|should|to)\b|\bwhat\s+(?:do|should|would)\s+(?:i|we)\b",
            r"\b(?:should|shall)\s+(?:i|we|you)\b|\b(?:did|have|were)\s+you\b(?:\s+[\w']+){0,2}?\s+(?:do|finish)",
            # Their own work, or teaching: "let me do it", "I'll finish it", "help me do this", "walk me through it",
            # "show me how", "explain", "I'm going to do this"
            r"\b(?:let\s+me|i'll|ill|i\s+will|i'm\s+going\s+to|im\s+going\s+to|i\s+(?:want|need|have)\s+to|"
            r"remind\s+me\s+to)\s+(?:[\w']+\s+){0,2}?" + _DOING + r"\b",
            r"\bhelp\s+me\b|\bwalk\s+me\s+through\b|\bshow\s+me\b|\bteach\s+me\b|\bexplain\b|\bguide\s+me\b|"
            r"\bhint\b|\btutor\b|\bquiz\s+me\b",
            # A condition: "ask me first", "only do it when I say", "not until I check", "do it once I'm back"
            r"\bask\s+(?:me\s+)?(?:first|before)\b|\bonly\b(?:\s+[\w']+){0,6}?\s+(?:when|if|after)\s+i\b",
            r"\b(?:when|once|after|before|if|until|unless|while)\b|\b(?:tonight|tomorrow|later|this\s+evening|"
            r"in\s+the\s+morning|on\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|in\s+\w+\s+"
            r"(?:seconds?|minutes?|hours?|days?))\b",
            # Somebody else's words, a why, or encouragement: "he said just do it", "why would you do this", "you can
            # do this", "could you do this faster than me?"
            r"\b(?:said|says|told|tells|asked)\b|\bwhy\b|\bfaster\s+than\b|\bi\s+believe\s+in\s+you\b",
            # Doing it together, or wondering whether it's allowed: "do this with me", "is it cheating"
            r"\bwith\s+me\b|\btogether\b|\bcheat\w*\b|\ballowed\b",
            # Taken back: "fill this out? I don't think so", "can you take over? no, wait", "maybe not"
            r"\bno,?\s+wait\b|\bno\s+way\b|\bmaybe\s+not\b|\bi\s+don'?t\s+think\s+so\b|\bhold\s+on\b",
            # A quiz, test or exam: answering one can hand it in (a timed one submits itself)
            r"\b(?:quiz(?:zes)?|tests?|exams?|midterms?|finals?|graded)\b",
        )
    )
)


# Tools whose words never count while she's on a call: anyone in the channel could have said them.
NOT_ON_CALLS = frozenset({"start_screen_task"})


def asks_for_screen_work(text: str) -> bool:
    """Whether the user's own words plainly ask her to do the work in front of them herself: "look at my screen and
    go ahead and do this", "finish the lab for me", "can you fill this out", "take over"; not "how do I do this",
    "help me do this" or "walk me through it" (teaching), "let me do it", "don't do it", "ask me first", "do it later"
    or a request taken back."""
    said = " ".join(text.lower().translate(_APOSTROPHES).split())
    if not said or _TASK_ASKS.search(said) is None:
        return False
    return _TASK_REFUSES.search(said) is None and _RETRACTS.search(said) is None


# ---- submitting and sending, with the mouse and keyboard ----
#
# The mouse and keyboard tools ask before anything risky they're about to do (computer_control's look at what's
# under the mouse). Of those, only submitting or sending (the "send" kind: Submit, Send, Post...) can be approved by
# the user's words, when they asked for it ("fill it in and submit it", "send it to him"), asked for work that's done
# by submitting its answers ("do this problem set"), or said they don't need to be asked ("you don't need my
# approval, just keep going"). Buying, deleting, installing, passwords and commands are always asked.

_SENDING = r"(?:submit|send|post|turn\s+in|hand\s+in|hit\s+submit|click\s+submit|press\s+submit)"
_PERMISSION = (
    r"(?:approval|approve|permission|authori[sz]ation|authori[sz]e|ok|okay|confirmation|confirm|go-?ahead|a\s+yes)"
)
# Saying she needn't ask: "you don't need (my) approval", "you don't need authorization to submit these", "no need to
# ask". Taken out before looking for a refusal, so its own "don't" isn't read as "don't submit".
_WAIVES = (
    rf"\b(?:you\s+)?(?:don'?t|do\s+not|won'?t)\s+(?:even\s+)?(?:need|have)\s+(?:to\s+(?:ask|get|wait\s+for)\s+)?"
    rf"(?:(?:my|any|an?|the)\s+)?(?:{_PERMISSION})(?:\s+(?:from\s+me|to\s+{_SENDING}(?:\s+[\w']+){{0,3}}))?"
    rf"|\bno\s+need\s+(?:to\s+ask|for\s+(?:my\s+|any\s+)?{_PERMISSION})"
)
_SUBMIT_ASKS = re.compile(
    "|".join(
        (
            # "fill it in and submit it", "then send it", "please submit", "go ahead and post it", "submit the answer"
            rf"(?:^|[.,!?;]\s*|\b(?:and|then|please|just|also|go\s+ahead\s+and|can\s+you|could\s+you|you\s+can|to)\s+)"
            rf"{_SENDING}\b",
            # "click the submit button", "hit send", "press the send button"
            r"\b(?:click|hit|press|tap)\s+(?:on\s+)?(?:the\s+)?(?:submit|send|post)\b",
            # work that's done by submitting its answers: "do this problem set", "finish the quiz", "work through the
            # exercises"
            r"\b(?:do|finish|complete|work\s+through|go\s+through|answer|solve)\s+(?:all\s+(?:of\s+)?)?"
            r"(?:the|this|these|that|my|your)\s+(?:[\w'-]+\s+){0,2}?"
            r"(?:problem\s+sets?|problems|exercises?|questions?|quiz(?:zes)?|steps|lab|lessons?|modules?|assignments?|"
            r"homework|worksheet|practice)\b",
            # "work through everything on the left", "complete all the work", "do it all", "start the work"
            r"\b(?:do|finish|complete|work\s+through|go\s+through|get\s+through|start|keep\s+doing)\s+"
            r"(?:all\s+(?:of\s+)?)?(?:everything|it\s+all|all\s+of\s+it|(?:the|this|my|your|all\s+the|that)\s+work)\b",
            # no need to ask: "you don't need (my) approval", "no need to ask", "don't ask me every time", "without
            # asking", "you can just do it", "just keep clicking"
            _WAIVES,
            r"\b(?:don'?t|do\s+not|stop)\s+ask(?:ing)?\b(?!\s+(?:me\s+)?(?:first|before))",
            r"\bwithout\s+(?:asking|(?:my\s+|any\s+)?(?:approval|permission|confirmation)|my\s+(?:ok|okay))\b",
            r"\byou\s+(?:can|may)\s+just\s+(?:do\s+it|go|submit|send|click)\b",
            r"\bjust\s+(?:do\s+it|go\s+for\s+it|submit\s+it|send\s+it|keep\s+(?:clicking|going|doing|submitting))\b",
        )
    )
)
_SUBMIT_REFUSES = re.compile(
    "|".join(
        (
            # "don't submit it", "do not send that yet", "never post anything"
            rf"\b{_NEGATOR}\b(?:\s+(?!(?:and|but|then|so|or|because|if|when)\b)[\w']+){{0,3}}?\s+{_SENDING}\b",
            # "stop submitting", "quit sending things"
            r"\b(?:stop|quit|no\s+more)\s+(?:submitting|sending|posting)\b",
            # a condition, not a go-ahead: "ask me before you submit", "only submit when I say", "not until I check"
            r"\bask\s+(?:me\s+)?(?:first|before)\b|\bonly\b(?:\s+[\w']+){0,6}?\s+(?:when|if|after)\s+i\b",
            r"\b(?:until|unless)\s+i\b|\b(?:let\s+me|i'll|ill|i\s+will|i\s+want\s+to)\s+(?:[\w']+\s+){0,2}?"
            r"(?:submit|send|post|check|look|review)\b",
            # asking about it, not for it: "should I submit it?", "did you send it?", "what happens if I submit?"
            r"\b(?:should|shall)\s+(?:i|we)\b|\b(?:did|have|were)\s+you\b(?:\s+[\w']+){0,2}?\s+(?:submit|send|post)",
            r"\bwhat\s+(?:happens|would\s+happen)\b|\bhow\s+(?:do|does|can|would)\s+(?:i|we|you)\b",
        )
    )
)


def asks_to_send(text: str, args: Mapping[str, object] | None = None) -> bool:
    """Whether the user's own words ask her to submit or send, or say she needn't ask (see above): True for "fill in
    the blanks and submit", "do this problem set", "you don't need approval, just keep clicking through"; False for
    "don't submit it", "ask me before you send anything", "should I submit it?", or a request taken back."""
    said = " ".join(text.lower().translate(_APOSTROPHES).split())
    if not said or _SUBMIT_ASKS.search(said) is None:
        return False
    return not withdraws_send(said)


def withdraws_send(text: str) -> bool:
    """Whether the words hold submitting back ("don't submit it", "ask me before you send anything", "stop
    submitting", "let me check it first"), which also ends a standing approval to submit (``Assistant``)."""
    said = re.sub(_WAIVES, " ", " ".join(text.lower().translate(_APOSTROPHES).split()))
    return _SUBMIT_REFUSES.search(said) is not None or _RETRACTS.search(said) is not None


# Commands in a terminal (the "command" kind) are asked one by one, with the exact text: one yes is never a free
# shell. Only when the user says so in plain words ("you can run commands in this terminal without asking", "don't
# ask before each command") does she run them in that terminal for the rest of the conversation, or when they answer
# one of her command questions with an impatient yes ("yes, stop asking", ``Assistant``). Never for a command that
# could destroy or break things (computer_control's "danger" kind), which is always asked.

_COMMANDS = r"(?:commands?|terminal|shell|console|cloud\s*shell|command\s+line|scripts?)"
_UNASKED = r"without\s+(?:asking|(?:my\s+|any\s+)?(?:approval|permission|confirmation)|a\s+yes)"
_COMMAND_ASKS = re.compile(
    "|".join(
        (
            # "you can run commands in this terminal without asking", "just run the commands without asking me"
            rf"\b{_COMMANDS}\b(?:\s+[\w']+){{0,6}}?\s+{_UNASKED}\b",
            # "don't ask before each command", "stop asking me about the commands", "no need to ask for every command"
            rf"\b(?:don'?t|do\s+not|stop|quit|no\s+need\s+to)\s+(?:keep\s+)?ask(?:ing)?\b(?:\s+[\w']+){{0,5}}?\s+"
            rf"{_COMMANDS}\b",
            # "you don't need my approval for the commands", "you don't need to ask to run commands"
            rf"\b(?:you\s+)?(?:don'?t|do\s+not)\s+(?:even\s+)?need\s+(?:to\s+ask\b|(?:(?:my|any|an?)\s+)?{_PERMISSION}\b)"
            rf"(?:\s+[\w']+){{0,5}}?\s+{_COMMANDS}\b",
            # "you have my permission to run commands", "you're allowed to run the terminal commands"
            rf"\b(?:you\s+have\s+(?:my\s+)?(?:permission|approval|the\s+go-?ahead)|you'?re\s+allowed)\b"
            rf"(?:\s+[\w']+){{0,5}}?\s+{_COMMANDS}\b",
        )
    )
)
_COMMAND_REFUSES = re.compile(
    "|".join(
        (
            # a condition, not a go-ahead: "ask me before you run anything", "ask me first", "not until I check"
            r"\bask\s+(?:me\s+)?(?:first|before)\b|\b(?:until|unless)\s+i\b",
            r"\b(?:let\s+me|i'll|i\s+will|i\s+want\s+to)\s+(?:[\w']+\s+){0,2}?(?:check|look|review|run|type)\b",
            # "stop running commands", "don't run anything else", "no more commands"
            rf"\b(?:stop|quit|don'?t|do\s+not|never)\s+(?:running|run|typing|type|entering)\b|\bno\s+more\s+{_COMMANDS}\b",
            # asking about it: "should I run it?", "how do I run this?"
            r"\b(?:should|shall)\s+(?:i|we)\b|\bhow\s+(?:do|does|can|would)\s+(?:i|we|you)\b",
        )
    )
)


def asks_to_run_commands(text: str, args: Mapping[str, object] | None = None) -> bool:
    """Whether the user's own words say she needn't ask before each command (see above): True for "you can run
    commands in this terminal without asking", "don't ask before every command"; False for a plain "run it", for
    "ask me before you run anything", or a request taken back."""
    said = " ".join(text.lower().translate(_APOSTROPHES).split())
    if not said or _COMMAND_ASKS.search(said) is None:
        return False
    return not withdraws_commands(said)


def withdraws_commands(text: str) -> bool:
    """Whether the words hold commands back ("ask me first", "stop running commands", "let me check"), which also
    ends a standing approval to run them (``Assistant``)."""
    said = _COMMAND_ASKS.sub(" ", " ".join(text.lower().translate(_APOSTROPHES).split()))
    return _COMMAND_REFUSES.search(said) is not None or _RETRACTS.search(said) is not None


# ---- downloading a file ----
#
# "Download the Qwen 9B Q6 model", "grab that GGUF again", "redownload qwen3.5 9b": a request to download, naming the
# very file by a word of its address (never by the name she picked to save it as: that's hers, not theirs). A size or
# version they name ("9B", "Q6", "v2") must be the one in the address. A download of a program or one after outside
# content is always asked (``_ONLY_FOR``). Questions ("what happened to the qwen download?"), talk about a download
# that exists ("resume the qwen download", "the qwen download died"), "don't download anything" or a request taken
# back never count; her own offer ("want me to download Qwen 9B?") does.
_DOWNLOAD_ASKS = re.compile(r"\b(?:re-?)?download(?:ing)?\b|\b(?:grab|fetch)\b|\bpull\s+(?:it\s+|that\s+)?down\b")
_DOWNLOAD_REFUSES = re.compile(
    "|".join(
        (
            r"\b(?:don'?t|do\s+not|never|stop|quit|no\s+more)\s+(?:[\w']+\s+){0,2}?(?:re-?)?download",
            r"\bask\s+(?:me\s+)?(?:first|before)\b|\b(?:until|unless)\s+i\b",
            r"\b(?:should|shall|can|could|do)\s+(?:i|we)\b|\bhow\s+(?:do|does|can|would|long|much|far|is)\b",
            r"\b(?:did|have|has|were|was|is|are)\s+(?:you|it|the|my|that|this)\b(?:\s+[\w']+){0,3}?\s+(?:re-?)?download",
            # a question, or talk about a download that's already there
            r"^(?:[\w'-]+,\s*)?(?:what|where|why|when|which|who|whose|is|are|was|were|did|does|has|have|how)\b",
            r"\b(?:resume|resuming|status|happened|progress|died|dead|stuck|stalled|failed|finished|done|going|"
            r"cancel\w*|stop\w*|pause\w*|delete\w*)\b",
        )
    )
)
_OFFER = re.compile(r"\b(?:want|like)\s+me\s+to\b")
# Words that don't pick out a file: "download the model file for me", "download the zip".
_NOT_A_NAME = frozenset(
    """a an the it its this that these those my me for to of and or from on in at with again please now
    file files model models version latest new one thing download redownload grab fetch pull down get
    https http www com org net io main resolve blob raw releases release
    zip gz tar tgz xz bz2 7z rar exe msi bin gguf safetensors pt pth onnx ckpt iso img pdf csv json txt mp4 mp3
    mkv wav dmg apk""".split()
)
# A size or version: "9b", "4b", "q6", "q4_k_m", "v2", "3.5".
_SPEC = re.compile(r"^(?:\d+(?:\.\d+)?[bkmgt]|q\d\w*|v\d[\w.]*|\d+(?:\.\d+)+)$")


def _name_words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9.]+", text.lower()) for w in [w.strip(".")] if w and w not in _NOT_A_NAME}


def _address_words(url: str) -> set[str]:
    """The words of the address's host and path (not its query), each also split at dots: "qwen3.5" gives "qwen3.5",
    "qwen3" and "5"."""
    host_and_path = re.sub(r"^[a-z]+://", "", url.lower()).split("?", 1)[0].split("#", 1)[0]
    words = _name_words(host_and_path)
    return words | {part for word in words for part in word.split(".") if part and part not in _NOT_A_NAME}


def _same_spec(word: str, token: str) -> bool:
    """ "9b" in "9b" or "9b-instruct", "3.5" in "qwen3.5"; never "4b" in "14b"."""
    if token == word or token.startswith(word):
        return True
    return token.endswith(word) and token[-len(word) - 1].isalpha()


def asks_to_download(text: str, args: Mapping[str, object] | None = None) -> bool:
    """Whether the user's own words ask her to download this very file (``args``: the call's ``url``): a download
    verb, a word of theirs that is (or begins, at three letters or more) a word of the address, and every size or
    version they name in it. True for "download the Qwen 9B Q6 model" with ``.../Qwen3.5-9B-Q6_K.gguf``; False for
    "download that model" (no name), "download qwen 4B" (another size), "should I download it?", "what happened to
    the qwen download", "don't download anything" or a request taken back."""
    said = " ".join(text.lower().translate(_APOSTROPHES).split())
    if not said or _DOWNLOAD_ASKS.search(said) is None:
        return False
    if _DOWNLOAD_REFUSES.search(said) is not None or _RETRACTS.search(said) is not None:
        return False
    if "?" in said and _OFFER.search(said) is None:
        return False
    target = _address_words(str((args or {}).get("url") or ""))
    if not target:
        return False
    words = _name_words(said)
    for word in words:
        if _SPEC.match(word) and not any(_same_spec(word, t) for t in target):
            return False
    return any(word in target or (len(word) >= 3 and any(t.startswith(word) for t in target)) for word in words)


# Saving one of her skills (``octavia.skills``): "save that as a skill", "keep it as a skill", "turn the skill back on".
# "make it a skill", "save the theme skill". Not "don't save it as a skill", "maybe save it later", "delete that
# skill", "what skills do you have?", "add a skill to my resume", "keep my skills sharp", "save all skills".
_SKILL_ASKS = re.compile(
    r"\b(?:save|keep|store)\b(?:\s+[\w'-]+){0,4}?\s+as\s+(?:(?:an?|your|my)\s+)?(?:new\s+)?skill\b"
    r"|\bmake\s+(?:it|that|this|them|those)\s+(?:into\s+)?(?:an?\s+)?(?:new\s+)?skills?\b"
    r"|\b(?:save|keep)\s+(?:the|that|this)\s+(?:[\w'-]+\s+){0,3}?skill\b"
    r"|\bturn\b(?:\s+[\w'-]+){0,5}?\s+skill\b(?:\s+[\w'-]+){0,4}?\s+back\s+on\b"
)
_VERSION_SAID = re.compile(r"\bversion\s+(\d+)\b")
_SKILL_REFUSES = re.compile(
    r"\b(?:don't|dont|do\s+not|never|no|not|stop|cancel|forget|delete|drop|remove|later|tomorrow|unless|if|maybe|"
    r"what|which|how|why|whether)\b|\?"
)


def asks_to_save_skill(text: str, args: Mapping[str, object] | None = None) -> bool:
    """Whether the user plainly asks her to save one of her skills (or turn one back on). Bringing back an older
    version (the call's ``version``) only when they name that very version: "bring back version 2 of it"."""
    said = " ".join(text.lower().translate(_APOSTROPHES).split())
    version = (args or {}).get("version") or 0
    if version:
        return bool(re.search(rf"\bversion\s+{int(version)}\b", said)) and not _SKILL_REFUSES.search(said)  # type: ignore[call-overload]
    if _VERSION_SAID.search(said):
        return False
    return bool(_SKILL_ASKS.search(said)) and not _SKILL_REFUSES.search(said) and not _RETRACTS.search(said)


# The tools whose call the user's words can approve, and how to tell they asked. Fixed: nothing else can skip its
# question, whatever it declares.
ASKED_FOR: Mapping[str, Callable[..., bool]] = MappingProxyType(
    {
        "look_at_screen": asks_to_look,
        "find_on_screen": asks_to_look,
        "install_improvement": asks_to_install,
        "build_improvement": asks_to_build,
        "click": asks_to_send,
        "type_text": asks_to_send,
        "press_keys": asks_to_send,
        "restart_now": asks_to_restart,
        "shut_down_self": asks_to_shut_down,
        "start_screen_task": asks_for_screen_work,
        "start_download": asks_to_download,
        "save_skill": asks_to_save_skill,
    }
)
# The tools whose words count only for some kinds of risky call their own look found (the start of
# ``RiskFinding.scope``, before any ":"), and the words for each: the mouse and keyboard ones only for submitting or
# sending, never buying, deleting, installing, a password or a command.
_ONLY_FOR: Mapping[str, Mapping[str, Callable[..., bool]]] = MappingProxyType(
    {
        **{name: MappingProxyType({"send": asks_to_send}) for name in ("click", "type_text", "press_keys")},
        # Only a restart that cuts nothing off (its look flagged nothing): one that stops a build is always asked.
        "restart_now": MappingProxyType({"": asks_to_restart}),
        # Only a shutdown at the PC that cuts nothing off: a remote one, or one that stops a build, is always asked.
        "shut_down_self": MappingProxyType({"": asks_to_shut_down}),
        # Only a start its own look flags nothing about.
        "start_screen_task": MappingProxyType({"": asks_for_screen_work}),
        # Only a download its own look flags nothing about: never a program, never after outside content.
        "start_download": MappingProxyType({"": asks_to_download}),
    }
)
# The matchers that judge the call itself (its arguments) as well as the words: an install must be of what was asked
# for ("install number 3" doesn't cover number 5, "install it" doesn't cover all of them).
_JUDGES_CALL = frozenset({"install_improvement", "start_download", "save_skill"})
# Where each of those tools is defined: its module, its class and the module's file (next to this one). A tool
# only counts when it's that very class, loaded from that very file: another one that takes the name (a new
# extension that loads first), inherits from it, forges its module and name, or is loaded from a package that
# shadows the file is asked as usual. Those modules are sensitive files (``evolve.policy.SENSITIVE``), so a change
# to what they do is always shown to the user before it's installed.
_DEFINED_IN: Mapping[str, tuple[str, str, str]] = MappingProxyType(
    {
        "look_at_screen": ("octavia.tools.extensions.screen_capture", "LookAtScreen", "extensions/screen_capture.py"),
        "find_on_screen": ("octavia.tools.extensions.screen_pointing", "FindOnScreen", "extensions/screen_pointing.py"),
        "install_improvement": ("octavia.evolve.tools", "InstallImprovement", "../evolve/tools.py"),
        "build_improvement": ("octavia.evolve.tools", "BuildImprovement", "../evolve/tools.py"),
        "click": ("octavia.tools.extensions.computer_control", "Click", "extensions/computer_control.py"),
        "type_text": ("octavia.tools.extensions.computer_control", "TypeText", "extensions/computer_control.py"),
        "press_keys": ("octavia.tools.extensions.computer_control", "PressKeys", "extensions/computer_control.py"),
        "restart_now": ("octavia.tools.extensions.restart", "RestartNow", "extensions/restart.py"),
        "shut_down_self": ("octavia.tools.extensions.shutdown", "ShutDownSelf", "extensions/shutdown.py"),
        "start_screen_task": ("octavia.tools.extensions.screen_tasks", "StartScreenTask", "extensions/screen_tasks.py"),
        "start_download": ("octavia.tools.extensions.long_jobs", "StartDownload", "extensions/long_jobs.py"),
        "save_skill": ("octavia.tools.extensions.skills", "SaveSkill", "extensions/skills.py"),
    }
)
_HERE = Path(__file__).resolve().parent


# ---- saying yes to her own offer ----
#
# Some announcements offer one exact call ("My build of X was cut off when I stopped. Want me to start it again?",
# ``Offer``): answering it with a plain yes is the approval of that call, so it isn't asked a second time. Only the
# tools listed here can be offered, and only when they're the very classes named in ``_DEFINED_IN``; the assistant
# holds an offer for the next turn only, and matches the call's arguments against it (``Assistant.offered``).
OFFERABLE = frozenset({"build_improvement", "save_skill"})

_YES = r"(?:yes|yeah|yea|yep|yup|sure|ok|okay|alright|all\s+right|absolutely|definitely|of\s+course|please|go\s+ahead)"
_YES_TAIL = (
    r"(?:please|thanks|thank\s+you|go\s+ahead|go\s+for\s+it|do\s+it|do\s+that|start\s+it(?:\s+again)?|"
    r"restart\s+it|build\s+it(?:\s+again)?|try\s+(?:it\s+)?again|sounds\s+good|that'?d\s+be\s+great|why\s+not)"
)
# The whole answer is a yes: "Yes.", "Yeah, go ahead.", "Sure, start it again please", "Octavia, yes". Anything more
# ("yes, but change the title first") is a request of its own, and is asked as usual.
_PLAIN_YES = re.compile(
    rf"^(?:[\w'-]+,\s*)?{_YES}(?:[\s,.!]+(?:{_YES}|{_YES_TAIL}))*[\s.!]*$"
    rf"|^(?:[\w'-]+,\s*)?{_YES_TAIL}(?:[\s,.!]+(?:{_YES}|{_YES_TAIL}))*[\s.!]*$"
)
_OFFERED_IN: Mapping[str, tuple[str, str, str]] = MappingProxyType(
    {
        "build_improvement": ("octavia.evolve.tools", "BuildImprovement", "../evolve/tools.py"),
        "save_skill": ("octavia.tools.extensions.skills", "SaveSkill", "extensions/skills.py"),
    }
)


def says_yes(text: str) -> bool:
    """Whether the user's whole answer is a plain yes ("Yes.", "Yeah, go ahead", "Sure, start it again"), with
    nothing that says no or takes it back ("yes... actually no", "not now")."""
    said = " ".join(text.lower().translate(_APOSTROPHES).split())
    return bool(said) and _PLAIN_YES.match(said) is not None and _RETRACTS.search(said) is None


# ---- saying yes to what she asked, in her own words ----
#
# A "yes" has no words of its own to judge: it means whatever she just put to the user ("Want me to install them?",
# "If you want, I'll put both updates in during that restart."). So when the user's whole answer is a plain yes, her
# own question is read as their request (``Assistant.offered``), by the same matchers as above. Only what she said or
# showed them since they last spoke, and of it only the sentences that put something to them: a question, or an
# offer ("if you want, ...", "want me to ...", "let me know if ...").
_PUTS_TO_USER = re.compile(
    r"\?\s*$|\b(?:if\s+you(?:'d|\s+would)?\s+(?:want|like)|want\s+me\s+to|shall\s+i|should\s+i|"
    r"just\s+say(?:\s+the\s+word)?|say\s+(?:yes|the\s+word)|let\s+me\s+know\s+if)\b"
)
_SENTENCE = re.compile(r"[^.!?]+(?:[.!?]+|$)")


def questions_in(said: Sequence[str]) -> list[str]:
    """The sentences of what she said (``said``: her replies and announcements, oldest first) that put something
    to the user, questions and offers, newest last: "I'm not building anything. Want me to restart now?" gives
    ["Want me to restart now?"]."""
    found: list[str] = []
    for text in said:
        clean = " ".join(text.translate(_APOSTROPHES).split())
        for m in _SENTENCE.finditer(clean):
            sentence = m.group(0).strip()
            if sentence and _PUTS_TO_USER.search(sentence.lower()):
                found.append(sentence)
    return found


# ---- the steps an approved call takes ----
#
# A yes is to what the user wants done, not to one call: when installing updates they said yes to finds one clashes
# with her newer code ("conflicts with newer changes... Ask me to build it again"), rebuilding that very update is how
# the install they approved gets done, and asking "want me to build it?" right after their yes is the question they
# hate. Only rebuilding an existing improvement (never a new request) that the approved install was of (the ones it
# says it tried, ``ToolResult.data``), in the same turn, with no other tool run since (``Assistant.already_approved``).


def steps_after(tool: str, args: Mapping[str, object]) -> Callable[[str, Mapping[str, object]], bool] | None:
    """What carrying out a call of ``tool`` that ran may also take, as a test of a later call (its tool and
    arguments); None when it takes nothing else. ``args``: what the call says it was about (an install: the
    ``improvement_ids`` it tried, every one by number)."""
    if tool != "install_improvement":
        return None
    given = args.get("improvement_ids") or []
    ids = {i for i in given if isinstance(i, int) and not isinstance(i, bool)} if isinstance(given, list) else set()
    if not ids:
        return None

    def rebuilds(name: str, call: Mapping[str, object]) -> bool:
        target = call.get("improvement_id")
        if name != "build_improvement" or not isinstance(target, int) or isinstance(target, bool):
            return False
        if call.get("request"):
            return False  # a new change to her code, not the update that was approved
        return target in ids

    return rebuilds


def offer_matcher_for(tool: Tool) -> Callable[[str], bool] | None:
    """How to tell the user said yes to an offer of a call of ``tool``; None when it can't be offered (asked as
    usual). Like ``matcher_for``, only for the very classes listed, loaded from their own files."""
    if tool.name not in OFFERABLE or not _is_the_real(tool, _OFFERED_IN.get(tool.name)):
        return None
    return says_yes


def _is_the_real(tool: Tool, defined_in: tuple[str, str, str] | None) -> bool:
    if defined_in is None:
        return False
    module_name, class_name, file_name = defined_in
    module = sys.modules.get(module_name)
    if module is None or type(tool) is not getattr(module, class_name, None):
        return False
    return _same_file(getattr(module, "__file__", None), _HERE / file_name)


def matcher_for(
    tool: Tool, args: Mapping[str, object] | None = None, flagged: str | None = None
) -> Callable[[str], bool] | None:
    """How to tell whether the user's words ask for a call of ``tool`` with these ``args`` (the call's own
    arguments, for the matchers that judge them too); None when they never can (always asked). ``flagged``: the kind
    of risky thing the tool's own look found this call does (``RiskFinding.scope``), or None when it found nothing:
    some tools' calls can be approved by words only for some kinds (``_ONLY_FOR``)."""
    matcher = ASKED_FOR.get(tool.name)
    defined_in = _DEFINED_IN.get(tool.name)
    if matcher is None or defined_in is None:
        return None
    only = _ONLY_FOR.get(tool.name)
    if only is not None:
        kind = (flagged or "").partition(":")[0]
        if kind not in only:
            return None
        matcher = only[kind]
    module_name, class_name, file_name = defined_in
    module = sys.modules.get(module_name)
    if module is None or type(tool) is not getattr(module, class_name, None):
        return None
    if not _same_file(getattr(module, "__file__", None), _HERE / file_name):
        return None
    if tool.name in _JUDGES_CALL:
        return partial(matcher, args=dict(args or {}))
    return matcher


def _same_file(path: object, expected: Path) -> bool:
    if not isinstance(path, str):
        return False
    try:
        return Path(path).resolve() == expected.resolve()
    except (OSError, RuntimeError, ValueError):
        return False
