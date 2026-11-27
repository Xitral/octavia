"""Her voice's tone cues, placed sentence by sentence the way a person would say it (``tts.fish_audio.place_cues``).

Fish S2 performs a cue in square brackets at the word it sits on: a tone at the start of a sentence ([excited]), a
laugh or a breath where it happens ([chuckling], [inhale]), a stressed word ([emphasis]), a beat ([break]). Her brain
may write a few itself, but live it did in 13 of 137 replies (2026-11-22 to 11-23): a 47k-token prompt about
everything else leaves little room for how a sentence should sound. So a quick Haiku session of its own marks up each
sentence of a reply after the first, with the reply so far for flow, while the sentence before it is still playing.

Nothing here can change what she says, and nothing waits on it:

* ``accept`` takes the marked-up sentence only when its words and punctuation are exactly the original's, and keeps
  only known cues in the places they work (tones at the start, moments and beats inside, never at the end), at most
  one of each kind per sentence, and not the tone the sentence before opened with. Anything else: the original.
* ``SpeechStream`` asks for a placement as each sentence is queued and uses it only if it's back before that sentence
  has to start synthesizing to play on time (what's still queued to play, less the voice's first-audio time). The
  first sentence of a reply, said into silence, is never placed: there's no time.

The user asked for it (2026-11-24): "a fast reasoning engine ... that chooses where to place modifiers". Small local
models (Qwen3.5 4B, Gemma 4 E4B, 0.2 s a sentence) put [confident] and [playful] on nearly every sentence and tones
mid-sentence; Haiku placed them like a reader would (1.4 s a sentence, behind playback). D-041.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# What the placer may use: tones for a sentence's start, moments for where they happen, beats for a pause.
TONES = frozenset({
    "excited", "surprised", "confident", "proud", "playful", "sarcastic", "curious", "relaxed", "sympathetic",
    "determined", "disappointed", "soft tone", "whispering", "delighted", "worried",
})  # fmt: skip
MOMENTS = frozenset({"chuckling", "laughing", "sigh", "inhale", "exhale", "emphasis"})
BEATS = frozenset({"break", "long-break"})
PLACED_TAGS = TONES | MOMENTS | BEATS

_TAG = re.compile(r"\[([^\[\]]{1,30})\]")
_SENTENCE_END = re.compile(r"[.!?…]+[\"')]*(?=\s|$)")

SYSTEM_PROMPT = """You mark up one sentence of Octavia's spoken reply for her voice engine, so it sounds like a lively, \
confident young woman really talking, not reading aloud. She's quick, cocky and warm, and she jokes a lot.

Return the sentence with its words and punctuation exactly as given, adding cues in square brackets. A cue already \
there may stay or go.
- At the very start, a tone, only when the sentence has a clear feeling: [excited] [surprised] [delighted] \
[confident] [proud] [playful] [sarcastic] [curious] [relaxed] [sympathetic] [worried] [determined] [disappointed] \
[soft tone] [whispering]
- Inside the sentence, one moment right where it happens: [chuckling] or [laughing] just as something funny lands, \
[sigh] before a weary bit, [inhale] before a long thought, [exhale] after relief, [emphasis] just before the one word \
she'd stress.
- Inside the sentence, a beat where a person would pause: [break] just before a punchline or a real turn ("but", \
"except", "and then"), [long-break] for a dramatic one. A beat before every "and" sounds like reading: once or twice \
a reply at most.
- If something's funny, wry or a letdown, let her react to it: a [chuckling] as the joke lands, a [sigh] at the \
annoying bit, rather than a tone at the start.

Use at most one of each kind, and most sentences need one cue or two, some none: a plain fact or a question to the \
user often gets none. Confident and playful is how she always sounds, so [confident] and [playful] add little: use \
them rarely, never twice in a reply. A moment or a beat in the right spot does more than a tone at the start. Vary \
them across the reply, and never the tone the sentence before used. Never put a cue at the end. Never change, add or remove a word or punctuation mark. Reply with the \
sentence only.

Examples (sentence -> marked up):
Gotta be tacos al pastor, with the pork shaved right off the spit. -> [excited] Gotta be tacos al pastor, with the \
pork shaved right off the spit.
I tried, and it crashed twice, so that's fun. -> I tried, and it crashed twice, [chuckling] so that's fun.
The meeting is at three on Thursday. -> The meeting is at three on Thursday.
Honestly, I'd skip the second one. -> Honestly, I'd skip the [emphasis] second one.
Okay, so here's the thing about recursion. -> [inhale] Okay, so here's the thing about recursion.
It works, but only if you never touch it again. -> It works, [break] but only if you never touch it again.
What do you want to do next? -> What do you want to do next?
I can't believe they cancelled it. -> [disappointed] I can't believe they cancelled it."""


def words(text: str) -> str:
    """The text without its cues, whitespace evened out: what must not change."""
    return " ".join(_TAG.sub(" ", text).split())


def first_tone(text: str) -> str | None:
    """The tone ``text`` opens with, if any."""
    m = re.match(r"\s*\[([^\[\]]{1,30})\]", text)
    tag = m.group(1).strip().lower() if m else None
    return tag if tag in TONES else None


def closing_tone(text: str) -> str | None:
    """The tone the last sentence of ``text`` opens with, if any: the next sentence shouldn't open the same way."""
    starts = [0] + [m.end() for m in _SENTENCE_END.finditer(text) if text[m.end() :].strip()]
    return first_tone(text[starts[-1] :])


# The most of each a whole reply may have (``accept``'s ``spent``): past them a cue stops sounding like a person and
# starts sounding like a pattern. Measured 2026-11-24: placed one sentence at a time, Haiku put a [break] before
# nearly every "and" (14 in 30 sentences).
PER_REPLY = {"tone": 3, "moment": 3, "beat": 2}


def accept(
    original: str, placed: str | None, previous_tone: str | None = None, spent: dict[str, int] | None = None
) -> str:
    """``placed`` if it says exactly what ``original`` says, with its cues kept only where they work; else
    ``original``. Per sentence: one tone, at the start (not ``previous_tone`` for the first sentence, nor the
    sentence before's for the others); one moment and one beat, inside; nothing trailing at the end. A beat only
    right after a comma or the like (a pause mid-phrase sounds like a stall). ``spent``: what the reply has used so
    far, counted on and kept within ``PER_REPLY``."""
    if not placed or words(placed) != words(original):
        return original
    pieces: list[tuple[str, str]] = []  # ("tag", name) | ("text", words)
    pos = 0
    for m in _TAG.finditer(placed):
        if placed[pos : m.start()].strip():
            pieces.append(("text", placed[pos : m.start()]))
        pieces.append(("tag", m.group(1).strip().lower()))
        pos = m.end()
    if placed[pos:].strip():
        pieces.append(("text", placed[pos:]))

    out: list[str] = []
    budget = spent if spent is not None else {}
    tone_before = previous_tone
    started = False  # the sentence has words yet
    used: set[str] = set()  # kinds used in this sentence: "tone", "moment", "beat"
    opened: str | None = None  # this sentence's tone
    for i, (kind, value) in enumerate(pieces):
        if kind == "text":
            out.append(value)
            started = True
            # A sentence ending in this piece (with words after it or not) starts the next one afresh.
            ends = list(_SENTENCE_END.finditer(value))
            if ends and not value[ends[-1].end() :].strip():
                tone_before, started, used, opened = opened, False, set(), None
            elif ends:
                tone_before, used, opened = opened, set(), None
            continue
        rest = any(k == "text" for k, _ in pieces[i + 1 :])
        if value in TONES:
            if started or "tone" in used or value == tone_before or not rest:
                continue
            if budget.get("tone", 0) >= PER_REPLY["tone"]:
                continue
            used.add("tone")
            budget["tone"] = budget.get("tone", 0) + 1
            opened = value
        elif value in MOMENTS or value in BEATS:
            group = "moment" if value in MOMENTS else "beat"
            leading_ok = value in ("inhale", "sigh")  # a breath or a sigh may open a sentence
            if group in used or not rest or (not started and not leading_ok):
                continue
            if group == "beat" and not (out and out[-1].rstrip()[-1:] in ",;:—–"):
                continue
            if budget.get(group, 0) >= PER_REPLY[group]:
                continue
            used.add(group)
            budget[group] = budget.get(group, 0) + 1
        else:
            continue  # not one of hers: Fish would say it
        out.append(f"[{value}]")
    return " ".join(" ".join(out).split())


class CuePlacer:
    """One sentence at a time, on its own warm quick-voice session (``chat``: a ``FastChat`` with ``SYSTEM_PROMPT``)."""

    def __init__(self, chat: Any, timeout_s: float = 4.0) -> None:
        self.chat = chat
        self.timeout_s = timeout_s
        self.asked = 0
        self.used = 0
        self.late = 0  # came back after its sentence had to start
        self.refused = 0  # changed the words, or failed: the original was said

    @property
    def ready(self) -> bool:
        return bool(self.chat.ready)

    async def start(self) -> None:
        await self.chat.start()

    async def close(self) -> None:
        await self.chat.close()

    async def place(self, sentence: str, before: list[str]) -> str | None:
        """``sentence`` marked up, or None (not connected, failed, too slow). Raw: ``accept`` judges it."""
        if not self.ready:
            return None
        self.asked += 1
        so_far = " ".join(s for s in before if s.strip())
        prompt = (f"Her reply so far: {so_far}" if so_far else "This is the first sentence of her reply.") + (
            f"\n\nSentence: {sentence}"
        )
        try:
            out = await self.chat.ask(prompt, self.timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("Cue placement failed", exc_info=True)
            return None
        return out.strip().strip('"') if out else None
