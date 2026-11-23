"""Whether what she wrote just before a step (a tool call), or as a turn's first words, is a note to herself.

The model sometimes writes itself a working note between its tool calls ("Need to look at all screens", "Wrong match;
look.") although the system prompt asks it not to. Those are for the model, not the user, and saying them out loud
sounds broken, so the assistant leaves them unspoken. A line meant for the user ("Let me check the other window.",
"Here, after the second sentence.") still is: only a short line that doesn't talk to the user and reads like a jotting
(no closing punctuation, a semicolon, or a telegraphic start: "Need page 2.", "Need a fresh look") counts
as a note. Where something is on the screen in numbers is never said, whatever path it takes
(``tts.chunker.gives_screen_spot``).
"""

from __future__ import annotations

import re

NOTE_MAX_WORDS = 12  # longer than this, it's something she's telling the user, not a jotting

_ADDRESSES = re.compile(
    r"\b(?:i|i'm|i'll|i've|i'd|me|my|mine|we|we're|we'll|let's|us|our|you|you're|you'll|you've|your|yours)\b",
    re.IGNORECASE,
)
_ENDS_A_SENTENCE = re.compile(r"[.!?…][\"')\]]*$")
_TELEGRAPHIC = re.compile(
    r"^(?:(?:hmm+|ok(?:ay)?|ah|oh|right),?\s+)?(?:needs?\s+(?:to|a|an|another|more|page)|still\s+need|should|must|wrong|todo)\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_LOADING = re.compile(r"\b(?:schemas?|tool ?search|load(?:ing)?)\b", re.IGNORECASE)


def about_her_tools(text: str, tools: frozenset[str] | set[str] = frozenset()) -> bool:
    """A short line that only names her own tools ("Need show_visual schema.", "Need study_status first, and
    show_visual schema.", both said out loud as whole replies on 2026-11-22): never said, even as her last words.
    Only her real tool names (``tools``) count, so "a star schema" in a database lesson or snake_case in a Python one
    is said as ever."""
    line = " ".join(text.replace("’", "'").split())
    if not 0 < len(line.split()) <= NOTE_MAX_WORDS or _ADDRESSES.search(line) or line.endswith("?"):
        return False
    return any(word in tools for word in _WORD.findall(line))


def is_note_to_self(text: str) -> bool:
    """``text``, written right before a step, is a working note rather than something said to the user."""
    line = " ".join(text.replace("’", "'").split())
    if not line or len(line.split()) > NOTE_MAX_WORDS or _ADDRESSES.search(line) or line.endswith("?"):
        return False  # a question ("Need a hand?") is said to someone
    return ";" in line or not _ENDS_A_SENTENCE.search(line) or bool(_TELEGRAPHIC.search(line))
