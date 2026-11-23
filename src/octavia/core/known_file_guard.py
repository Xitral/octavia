"""She never asks the user to open a file she knows the way to.

Her fresh prompt says where the documents she's read are (``core.known_documents``), and that's what she goes by. This
is the guarantee behind it, for the turn she gets it wrong anyway. Like a claim her steps contradict (``core.claims``),
a sentence asking for a known file ("Can you open the PDF again?"), or saying she hasn't got it, is caught as it's
about to be said and replaced by a short line. When the turn ends, one more turn of her own runs straight away with
the file's path, so she reads it and carries on with what they asked.

It doesn't get in the way when asking is honest: when a read failed in this turn, or she read that file and wants
them looking at it too. A turn of her own is never checked, so what she does after being stopped can't
be stopped again. What she offers ("want me to open it?") isn't asking them. A file named some other way is a different
file, and a guess wrong in that direction costs only a moment: she's told it may not be the one, and can ask.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from octavia.core.claims import _SENTENCES_KEPT, USER_SOURCES
from octavia.core.events import BrainFinished, BrainStarted, EventBus, ToolCallFinished
from octavia.core.known_documents import KnownDocument, KnownDocuments, describe

log = logging.getLogger(__name__)

RECENT_S = 12 * 3600.0  # how long "the PDF", with no name, means the one she was just reading
HELD_LINE = "Hang on, I know where that is."
TURN = "(You stopped yourself asking the user for a file you can read yourself: read it and carry on.)"
NOTE = (
    'you were about to say "{sentence}" to the user, who heard "{line}" instead. You don\'t need them for this: read '
    "it yourself with read_document and carry on with what they said last{asked}, without going over this.\n"
    "What you've read that it may mean:\n{documents}\n"
    "Only if it's none of these, say so and ask."
)

_GENERIC = r"(?:pdf|file|document|doc)"  # says nothing of which one: the one she was just reading
_DOC = (
    rf"(?:{_GENERIC}|docx|slides?|deck|powerpoint|notes|handout|worksheet|exam|midterm|final|quiz|practice|lecture"
    r"|syllabus|chapter|paper|essay|report|spreadsheet|sheet|reading)"
)
_THE_DOC = rf"(?:the|that|this|your)\s+(?:[\w'-]+\s+){{0,3}}?{_DOC}\b(?!\s+(?:question|problem|section|part|answer))"
_IT = r"(?:it|that\s+one)\b"
_FETCH = (
    rf"(?:(?:re-?open|open|share|send|upload|paste|attach|drop|give\s+me|show\s+me)\s+(?:{_IT}|{_THE_DOC})"
    rf"|(?:pull|bring|put)\s+(?:up\s+|back\s+up\s+)?(?:{_IT}|{_THE_DOC})(?:\s+(?:back\s+)?(?:up|on))?)"
)
_ASKS = re.compile(
    r"\b(?:(?:can|could|would|will)\s+you|please|(?:need|want)\s+you\s+to|you(?:'ll|\s+will)?\s+(?:need|have)\s+to"
    rf"|if\s+you(?:\s+(?:can|could))?|once\s+you)\b[^.?!]*?\b{_FETCH}",
    re.I,
)
_LACKS = re.compile(
    r"\bI\s+(?:(?:don't|do\s+not|no\s+longer|didn't)\s+have|(?:can't|cannot|couldn't|could\s+not)\s+"
    rf"(?:find|get|locate|access|open|read|pull\s+up|bring\s+up)|lost)\s+(?:{_IT}|{_THE_DOC})",
    re.I,
)
_HER_OFFER = re.compile(r"\b(?:me\s+to|shall\s+i|should\s+i)\b", re.I)
_PDF = re.compile(r"\bpdf\b", re.I)
_SAYS_WHICH_NOT = re.compile(rf"(?:\b(?:the|that|this|your)\s+(?:same\s+|whole\s+)?{_GENERIC}\b|\b{_IT})", re.I)
_ANY_DOC = re.compile(rf"\b{_DOC}\b", re.I)


@dataclass
class _Turn:
    text: str
    source: str
    mentioned: bool = False  # a document's come up in what she's said, so "it" can be one
    read: set[str] = field(default_factory=set)  # read in this turn: asking for one now is for them to see it too
    failed: bool = False  # a read failed in this turn: asking now is honest
    held: dict[str, list[KnownDocument]] = field(default_factory=dict)


@dataclass(frozen=True)
class Owed:
    """The turn her stopped sentence is owed: its words and its note."""

    turn: str
    note: str


class KnownFileGuard:
    def __init__(self, events: EventBus, documents: KnownDocuments) -> None:
        self.documents = documents
        self._turn: _Turn | None = None
        self._owed: Owed | None = None
        events.subscribe(self._on_event, BrainStarted, ToolCallFinished, BrainFinished)

    def _on_event(self, event: Any) -> None:
        match event:
            case BrainStarted(text=text, source=source):
                self._turn, self._owed = _Turn(text, source), None
            case ToolCallFinished(name="read_document", ok=ok, data=data):
                if self._turn is not None:
                    if ok and data and data.get("path"):
                        self._turn.read.add(str(data["path"]))
                    elif not ok:
                        self._turn.failed = True
            case BrainFinished(error=None, interrupted=False):
                turn, self._turn = self._turn, None
                if turn is not None and turn.held:
                    self._owed = _owed_for(turn)
            case BrainFinished():
                self._turn = None

    def vet(self, text: str) -> str:
        """``text``, with each sentence asking for a file she can read herself replaced. Never in the way: a failure
        here says the sentence as written."""
        turn = self._turn
        if turn is None or turn.source not in USER_SOURCES or not text:
            return text
        try:
            return self._vetted(turn, text)
        except Exception:
            log.warning("Checking whether she was asking for a known file failed", exc_info=True)
            return text

    def _vetted(self, turn: _Turn, text: str) -> str:
        pieces = _SENTENCES_KEPT.split(text)
        changed = False
        for i in range(0, len(pieces), 2):
            sentence = pieces[i]
            turn.mentioned = turn.mentioned or _ANY_DOC.search(sentence) is not None
            if sentence.strip() in turn.held:  # the whole reply, after its pieces: the same line once
                pieces[i], changed = HELD_LINE, True
                continue
            if turn.failed or not _about_getting_a_file(sentence):
                continue
            found = [d for d in self._meant(sentence, turn) if d.path not in turn.read]
            if not found:
                continue
            log.info("Holding %r: she knows where it is (%s)", sentence.strip(), found[0].path)
            turn.held[sentence.strip()] = found
            pieces[i], changed = HELD_LINE, True
        return "".join(pieces) if changed else text

    def _meant(self, sentence: str, turn: _Turn) -> list[KnownDocument]:
        """What she's read that ``sentence`` means: by a word of its name, else, when it says only "the PDF" or "it"
        (with a document just mentioned), the newest."""
        named = [d for d in self.documents.named_in(sentence) if d.kind == "file"]
        if named:
            return named
        which = _SAYS_WHICH_NOT.search(sentence)
        if which is None or (which.group().casefold().startswith(("it", "that one")) and not turn.mentioned):
            return []
        recent = [d for d in self.documents.recent(RECENT_S) if d.kind == "file"]
        if _PDF.search(sentence):  # she said it's a PDF: not the Word file she read
            recent = [d for d in recent if os.path.splitext(d.name)[1].casefold() == ".pdf"]
        return recent[:3]

    def owed(self) -> Owed | None:
        """The turn to run now, once: when her last turn had a sentence stopped."""
        owed, self._owed = self._owed, None
        return owed


def _about_getting_a_file(sentence: str) -> bool:
    if _LACKS.search(sentence):
        return True
    return _ASKS.search(sentence) is not None and _HER_OFFER.search(sentence) is None


def _owed_for(turn: _Turn) -> Owed:
    sentence = next(iter(turn.held))
    seen: dict[str, KnownDocument] = {}
    for found in turn.held.values():
        seen.update({d.path: d for d in found})
    asked = f' ("{" ".join(turn.text.split())[:200]}")' if turn.text else ""
    listing = "\n".join(describe(d) for d in seen.values())
    return Owed(TURN, NOTE.format(sentence=sentence, line=HELD_LINE, asked=asked, documents=listing))
