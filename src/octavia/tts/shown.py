"""What she says out loud versus what her subtitles show.

Her replies are written for the ear ("x squared plus one"), which is hard to follow for a formula, code or an exact
figure. Where reading it helps, she writes both forms in one mark, ``[[shown|said]]``: her voice speaks the part after
the last ``|``, her subtitles (and anything that shows the reply as text) show the part before it, which may hold TeX
math (``$x^2 + 1$``) and ``code``. A mark with no ``|`` is shown and said alike.

A beat, ``{{step-id}}``, is neither said nor shown: it ties the sentence it's in to a step of an explainer on a panel
(``visuals/explainer.py``), which plays as that sentence starts. The chunker carries a sentence's beats on its
``Spoken`` segment.

A mark still being written (the reply streams in) is kept whole by the chunker, so a sentence break inside the TeX
never cuts it in two; one that's never closed reads as a mark to the end of the text. A "[[" in code, or one that
can't be a mark (a space after it, no bar or end soon enough), is just text.
"""

from __future__ import annotations

import re

# A mark's body never holds another "[[" (a stray one before a real mark must not swallow the text between them).
_MARK = re.compile(r"\[\[((?:(?!\[\[).)*?)\]\]", re.S)
# Code, where "[[" is just code (``m = [[1, 2]]``): fenced blocks (one still open runs to the end) and inline spans.
_CODE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.S)
# Spans of the shown text left exactly as written: math and code.
_VERBATIM = re.compile(r"\$\$.+?\$\$|\$[^$\n]+\$|`[^`\n]+`", re.S)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_CONTROL = re.compile(r"<\|[^|>]*\|>")
# A voice cue ([excited], [short pause]): lowercase words. [Ctrl], [1] or [sic.] stay.
_CUE = re.compile(r"\[[a-z][a-z ]{0,30}\]")
_HEADING = re.compile(r"(?m)^\s*#{1,6}\s*")
# A beat: an explainer step's id in double braces (never in code).
_BEAT = re.compile(r"\{\{([A-Za-z0-9_-]{1,40})\}\}[ \t]?")  # (a space after it goes with it)
_LIST_PREFIX = re.compile(r"(?m)^\s*[-*•]\s+")

# How long a mark may stay open while the chunker waits for its end, before its bar and in all: past these (or
# with a space right after the "[[") it's a stray "[[" (wiki talk, code), and her voice doesn't wait on it.
OPEN_BEFORE_BAR = 120
MAX_OPEN_MARK = 360


def _parts(body: str) -> tuple[str, str]:
    shown, bar, said = body.rpartition("|")
    if not bar:
        return body, body
    return shown, said


def _in_code(code: list[tuple[int, int]], at: int) -> bool:
    return any(s <= at < e for s, e in code)


def _code(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _CODE.finditer(text)] if "`" in text else []


def _closed(text: str, code: list[tuple[int, int]]) -> list[re.Match[str]]:
    return [m for m in _MARK.finditer(text) if not _in_code(code, m.start())]


def _open_at(text: str, closed: list[re.Match[str]], code: list[tuple[int, int]]) -> int | None:
    start = text.rfind("[[")
    if start < 0 or (closed and closed[-1].end() > start) or "]]" in text[start:] or _in_code(code, start):
        return None
    body = text[start + 2 :]
    if body[:1].isspace():
        return None
    bar = body.rfind("|")
    if len(body) > MAX_OPEN_MARK or (bar < 0 and len(body) > OPEN_BEFORE_BAR) or bar > OPEN_BEFORE_BAR:
        return None
    return start


def _resolve(text: str, which: int) -> str:
    code = _code(text)
    closed = _closed(text, code)
    start = _open_at(text, closed, code)
    out: list[str] = []
    last = 0
    for m in closed:
        out.append(text[last : m.start()])
        out.append(_parts(m.group(1))[which])
        last = m.end()
    if start is not None:  # still open (or never closed, at the end of a reply): a mark to the end
        out.append(text[last:start])
        out.append(_parts(text[start + 2 :])[which])
    else:
        out.append(text[last:])
    return "".join(out)


def said_form(text: str) -> str:
    """``text`` as her voice says it: each mark's spoken part, beats out."""
    text = without_beats(text)
    return _resolve(text, 1) if "[[" in text else text


def shown_form(text: str) -> str:
    """``text`` as it's shown: each mark's shown part, beats out. Code is left as written."""
    text = without_beats(text)
    return _resolve(text, 0) if "[[" in text else text


def beats_in(text: str) -> tuple[str, ...]:
    """The explainer steps ``text`` names with beats (``{{step-id}}``), in order; none inside code."""
    if "{{" not in text:
        return ()
    code = _code(text)
    return tuple(m.group(1) for m in _BEAT.finditer(text) if not _in_code(code, m.start()))


def without_beats(text: str) -> str:
    """``text`` with its beats taken out (code left as written)."""
    if "{{" not in text:
        return text
    code = _code(text)
    return _BEAT.sub(lambda m: m.group(0) if _in_code(code, m.start()) else "", text)


def open_mark(text: str) -> int | None:
    """Where a mark that isn't closed yet starts in ``text``, or None (none, or only a stray "[[")."""
    if "[[" not in text:
        return None
    code = _code(text)
    return _open_at(text, _closed(text, code), code)


def mark_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of every mark in ``text``; one still open runs past the end."""
    if "[[" not in text:
        return []
    code = _code(text)
    closed = _closed(text, code)
    spans = [(m.start(), m.end()) for m in closed]
    start = _open_at(text, closed, code)
    if start is not None:
        spans.append((start, len(text) + 1))  # past the end: a break at the very end is inside it too
    return spans


def inside(spans: list[tuple[int, int]], at: int) -> tuple[int, int] | None:
    """The span that position ``at`` falls strictly inside, if any."""
    return next(((s, e) for s, e in spans if s < at < e), None)


def as_mark(shown: str, said: str) -> str:
    """A mark that says ``said`` and shows ``shown`` (just ``said`` when they're the same, or ``shown`` couldn't sit
    in a mark whole)."""
    if not shown or shown == said or "[[" in shown or "]]" in shown or "|" in said:
        return said
    if said.endswith("]"):
        said += " "  # "[laughs]" then "]]" would close the mark a bracket early
    return f"[[{shown}|{said}]]"


def clean_for_screen(text: str) -> str:
    """Shown text for a subtitle: voice cues, control tokens, link targets and heading/list markers out; math and
    code untouched (a ``[0, 1]`` in TeX is an interval, not a cue)."""
    pieces: list[str] = []
    last = 0
    for m in _VERBATIM.finditer(text):
        pieces.append(_clean_plain(text[last : m.start()]))
        pieces.append(m.group(0))
        last = m.end()
    pieces.append(_clean_plain(text[last:]))
    return " ".join("".join(pieces).split())


def _clean_plain(text: str) -> str:
    text = _CONTROL.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _CUE.sub("", text)
    text = _HEADING.sub("", text)
    return _LIST_PREFIX.sub("", text)


def verbatim_parts(text: str) -> list[str]:
    """The math and code spans in shown text, without their delimiters."""
    return [m.group(0).strip("$`").strip() for m in _VERBATIM.finditer(text)]


class Spoken(str):
    """A segment of speech (the text her voice says) that also carries how it's shown, and the explainer steps its
    beats name."""

    shown: str
    beats: tuple[str, ...]

    def __new__(cls, said: str, shown: str, beats: tuple[str, ...] = ()) -> Spoken:
        obj = super().__new__(cls, said)
        obj.shown = shown
        obj.beats = tuple(beats)
        return obj

    def __getnewargs__(self) -> tuple[str, str, tuple[str, ...]]:  # type: ignore[override]  # copies keep both
        return (str(self), self.shown, self.beats)
