"""Text out of documents: PDF, Word (.docx), PowerPoint (.pptx), web pages, plain text, CSV and code.

Only the standard library is used, since none of the usual readers (pypdf, python-docx) is a dependency. Word and
PowerPoint files are zip archives of XML. A PDF's text is in its pages' content streams, mostly zlib-compressed, as
strings of character codes; each font's ToUnicode map says which characters its codes stand for, and a font without
one is read as Windows-1252, which is what simple fonts use. That covers what word processors, slide programs and
LaTeX export. A scan is only pictures of pages, and says so rather than coming back empty.

Everything here reads untrusted bytes: sizes are capped (a small zip or stream can inflate enormously) and a file
that breaks the reader comes back as a plain note, never an exception.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urljoin
from xml.etree import ElementTree

log = logging.getLogger(__name__)

MAX_INFLATED = 40_000_000  # bytes one compressed stream or archive member may grow to
MAX_PDF_PAGES = 400
MAX_PDF_OBJECTS = 300_000
MAX_TEXT = 2_000_000  # characters: reading stops once a document has this much

TEXT_EXTENSIONS = frozenset(
    ".txt .md .markdown .rst .csv .tsv .json .xml .yaml .yml .toml .ini .cfg .log .tex .bib .sql .py .java .c .h .cpp "
    ".cc .hpp .cs .js .jsx .ts .tsx .css .scss .php .rb .go .rs .kt .swift .scala .r .m .jl .lua .pl .sh .bash .bat "
    ".ps1 .asm .s .v .vhd .hs .ml .lisp .scm .dart .gradle .properties .srt .vtt".split()
)
IMAGE_EXTENSIONS = frozenset(".png .jpg .jpeg .gif .bmp .webp .tif .tiff .heic .heif .svg .ico".split())
OLD_OFFICE = {".doc": "Word", ".ppt": "PowerPoint", ".xls": "Excel", ".xlsx": "Excel", ".rtf": "Rich Text"}
MEDIA_EXTENSIONS = frozenset(".mp3 .mp4 .m4a .wav .mov .avi .mkv .webm .wmv".split())
ARCHIVE_EXTENSIONS = frozenset(".zip .7z .rar .tar .gz".split())


@dataclass(frozen=True)
class Extracted:
    """A document's text, or (``text`` empty) a plain reason there's none in ``note``."""

    text: str
    note: str = ""


def not_readable(name: str, content_type: str = "") -> str | None:
    """Why a file can't give any text, judged from its name and type alone (so it needn't be downloaded), or None."""
    ext = PurePosixPath(name.casefold()).suffix
    kind = content_type.split(";")[0].strip().casefold()
    if ext in IMAGE_EXTENSIONS or kind.startswith("image/"):
        return "It's an image, so it has no text to read."
    if ext in MEDIA_EXTENSIONS or kind.startswith(("audio/", "video/")):
        return "It's an audio or video file, so it has no text to read."
    if ext in OLD_OFFICE:
        return f"It's an {OLD_OFFICE[ext]} file in a format that can't be read here; opening it in Office works."
    if ext in ARCHIVE_EXTENSIONS:
        return "It's a compressed archive; its files can't be read from inside it here."
    return None


def extract_text(name: str, data: bytes, content_type: str = "") -> Extracted:
    """The text of a file called ``name``, chosen by its extension, its content type, then its first bytes."""
    if reason := not_readable(name, content_type):
        return Extracted("", reason)
    ext = PurePosixPath(name.casefold()).suffix
    kind = content_type.split(";")[0].strip().casefold()
    try:
        if ext == ".pdf" or kind == "application/pdf" or data[:1024].lstrip().startswith(b"%PDF-"):
            return pdf_text(data)
        if ext == ".docx" or kind.endswith("wordprocessingml.document"):
            return _docx(data)
        if ext == ".pptx" or kind.endswith("presentationml.presentation"):
            return _pptx(data)
        if ext in (".html", ".htm") or kind == "text/html":
            return _result(read_html(decode_text(data))[0])
        if ext == ".ipynb":
            return _notebook(data)
        if ext in TEXT_EXTENSIONS or kind.startswith("text/") or kind in ("application/json", "application/xml"):
            return _result(decode_text(data), keep_indent=True)
        if data and not _binary(data):
            return _result(decode_text(data), keep_indent=True)
    except Exception as exc:  # any file from anywhere: a broken one is a note, not a crash
        log.debug("Couldn't read %s: %s", name, exc, exc_info=True)
        return Extracted("", "It looks damaged or unusual, so its text couldn't be read.")
    shown = ext or "binary"
    return Extracted("", f"It's a {shown} file, which isn't a document with text to read.")


def decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _binary(data: bytes) -> bool:
    return b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff"))


def tidy(text: str, keep_indent: bool = False) -> str:
    """Runs of spaces made one and blank lines at most one in a row; code keeps its indentation."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if keep_indent:
        lines = [line.rstrip() for line in lines]
    else:
        lines = [re.sub(r"[ \t ​]+", " ", line).strip() for line in lines]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n").rstrip()


def _result(text: str, keep_indent: bool = False, empty: str = "It has no text in it.") -> Extracted:
    text = tidy(text, keep_indent)
    return Extracted(text) if text.strip() else Extracted("", empty)


# ---------------------------------------------------------------------------------------------------- HTML

_BLOCKS = frozenset(
    "p div section article header footer main aside nav h1 h2 h3 h4 h5 h6 ul ol table tr blockquote pre hr dl dt dd "
    "figure figcaption details summary".split()
)
_SKIPPED = frozenset({"script", "style", "noscript", "template"})


class _HtmlText(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.parts: list[str] = []
        self.links: list[str] = []
        self._skip = 0
        self._pre = 0
        self._anchors: list[tuple[str, int]] = []
        self._lists: list[list[int]] = []  # open lists, innermost last: [items so far, 1 when ordered]
        self._bullet = -1  # where the newest list item's text starts, until it has some

    def _bare_bullet(self) -> bool:
        """Whether a list item was just opened with nothing in it yet, so a paragraph inside it (``<li><p>``) stays
        on the bullet's line instead of leaving the bullet alone above its text."""
        return self._bullet >= 0 and not "".join(self.parts[self._bullet :]).strip()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED:
            self._skip += 1
            return
        found = {k: v or "" for k, v in attrs}
        for key in ("href", "src", "data-api-endpoint"):
            if found.get(key):
                self.links.append(found[key])
        if tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            marker = "-"
            if self._lists:
                self._lists[-1][0] += 1
                if self._lists[-1][1]:
                    marker = f"{self._lists[-1][0]}."
            self.parts.append(f"\n{marker} ")
            self._bullet = len(self.parts)
        elif tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag in _BLOCKS:
            if not self._bare_bullet():
                self.parts.append("\n\n" if tag not in ("tr", "dt", "dd") else "\n")
            if tag in ("ul", "ol"):
                start = found.get("start", "")
                self._lists.append([int(start) - 1 if start.isdigit() else 0, int(tag == "ol")])
        if tag == "pre":
            self._pre += 1
        elif tag == "a":
            self._anchors.append((found.get("href", ""), len(self.parts)))
        elif tag == "img":
            alt = " ".join(found.get("alt", "").split())
            self.parts.append(f"[image: {alt}]" if alt else "[image]")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "pre":
            self._pre = max(0, self._pre - 1)
        elif tag in ("ul", "ol") and self._lists:
            self._lists.pop()
        if tag == "a" and self._anchors:
            href, start = self._anchors.pop()
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                url = urljoin(self.base + "/", href) if self.base else href
                label = "".join(self.parts[start:]).strip()
                if not label:
                    self.parts.append(url)
                elif _squash(label) != _squash(url):
                    self.parts.append(f" ({url})")
            elif href.startswith("mailto:"):
                self.parts.append(f" ({href[7:]})")
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data if self._pre else re.sub(r"\s+", " ", data))


def _squash(text: str) -> str:
    return re.sub(r"^https?://|/$", "", text.strip().casefold())


def read_html(html: str, base: str = "") -> tuple[str, list[str]]:
    """Plain text of an HTML fragment, links kept in parentheses after their words, and every link, image source and
    Canvas API endpoint it holds (as written, relative ones too)."""
    parser = _HtmlText(base.rstrip("/"))
    parser.feed(html or "")
    parser.close()
    text = tidy("".join(parser.parts))
    text = re.sub(r"(?m)^\|\s*", "", text)
    return text, parser.links


# ---------------------------------------------------------------------------------------------------- Office


def _member(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > MAX_INFLATED:
        raise ValueError(f"{name} is too big")
    return archive.read(info)


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _docx(data: bytes) -> Extracted:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root = ElementTree.fromstring(_member(archive, "word/document.xml"))
    paragraphs = []
    for paragraph in root.iter(f"{_W}p"):
        pieces = []
        for node in paragraph.iter():
            if node.tag == f"{_W}t":
                pieces.append(node.text or "")
            elif node.tag == f"{_W}tab":
                pieces.append(" ")
            elif node.tag in (f"{_W}br", f"{_W}cr"):
                pieces.append("\n")
        text = "".join(pieces)
        if text.strip() and paragraph.find(f"{_W}pPr/{_W}numPr") is not None:
            text = "- " + text
        paragraphs.append(text)
    return _result("\n".join(paragraphs), empty="The document has no text in it.")


def _pptx(data: bytes) -> Extracted:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        slides = sorted(
            (int(m.group(1)), name)
            for name in archive.namelist()
            if (m := re.fullmatch(r"ppt/slides/slide(\d+)\.xml", name))
        )
        parts = []
        wrote = False
        for number, name in slides:
            root = ElementTree.fromstring(_member(archive, name))
            lines = ["".join(t.text or "" for t in p.iter(f"{_A}t")) for p in root.iter(f"{_A}p")]
            body = "\n".join(line for line in lines if line.strip())
            wrote = wrote or bool(body)
            parts.append(f"[Slide {number}]\n{body}" if body else f"[Slide {number}] (no text)")
    if not slides:
        return Extracted("", "The presentation has no slides.")
    if not wrote:
        return Extracted("", "Its slides have no text, only pictures.")
    return Extracted(tidy("\n\n".join(parts)))


def _notebook(data: bytes) -> Extracted:
    notebook = json.loads(decode_text(data))
    cells = notebook.get("cells") if isinstance(notebook, dict) else None
    if not isinstance(cells, list):
        return _result(decode_text(data), keep_indent=True)
    parts = []
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        source = cell.get("source")
        text = "".join(str(s) for s in source) if isinstance(source, list) else str(source or "")
        if text.strip():
            parts.append(f"[{cell.get('cell_type') or 'cell'} cell]\n{text.strip()}")
    return _result("\n\n".join(parts), keep_indent=True, empty="The notebook has no cells with text.")


# ---------------------------------------------------------------------------------------------------- PDF


class _Name(str):
    """A PDF name (/Type), without its slash."""


class _Op(str):
    """A bare keyword: an operator in a content stream, or obj, R and the like in the file's structure."""


@dataclass(frozen=True)
class _Ref:
    number: int


class _End(Exception):
    pass


_REGULAR = rb"[^\s\x00()<>\[\]{}/%]"
_TOKEN = re.compile(
    rb"(?:[\s\x00]|%[^\r\n]*)*(?:(?P<dopen><<)|(?P<dclose>>>)|(?P<aopen>\[)|(?P<aclose>\])|(?P<hex><[0-9A-Fa-f\s]*>)"
    rb"|(?P<str>\()|(?P<name>/" + _REGULAR + rb"*)|(?P<word>" + _REGULAR + rb"+)|(?P<other>[{}<>)]))"
)
_NUMBER = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")
_SPECIAL = re.compile(rb"[()\\]")
_ESCAPES = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
_INLINE_END = re.compile(rb"[\s\x00]EI(?=[\s\x00]|$)")


class _Lexer:
    """PDF syntax, from ``pos``: numbers, strings (as bytes), names, arrays, dictionaries, references, keywords."""

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    def token(self) -> tuple[str, Any] | None:
        match = _TOKEN.match(self.data, self.pos)
        if match is None or match.lastgroup is None:
            return None
        self.pos = match.end()
        kind = match.lastgroup
        raw = match.group(kind)
        if kind == "str":
            return "value", self._literal()
        if kind == "hex":
            digits = re.sub(rb"\s", b"", raw[1:-1])
            return "value", bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode("ascii"))
        if kind == "name":
            name = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw[1:])
            return "value", _Name(name.decode("latin-1"))
        if kind == "word":
            if _NUMBER.fullmatch(raw):
                return "value", float(raw) if b"." in raw else int(raw)
            if raw in (b"true", b"false"):
                return "value", raw == b"true"
            if raw == b"null":
                return "value", None
            return "op", _Op(raw.decode("latin-1"))
        return kind, None

    def _literal(self) -> bytes:
        data, pos, depth, out = self.data, self.pos, 1, bytearray()
        while True:
            match = _SPECIAL.search(data, pos)
            if match is None:
                out += data[pos:]
                self.pos = len(data)
                return bytes(out)
            out += data[pos : match.start()]
            char, pos = data[match.start()], match.end()
            if char == 0x5C:  # backslash
                if pos >= len(data):
                    continue
                escaped = data[pos]
                if 0x30 <= escaped <= 0x37:
                    digits = re.match(rb"[0-7]{1,3}", data[pos : pos + 3])
                    assert digits is not None
                    out.append(int(digits.group(0), 8) & 0xFF)
                    pos += len(digits.group(0))
                elif escaped in (0x0A, 0x0D):
                    pos += 2 if data[pos : pos + 2] == b"\r\n" else 1
                else:
                    out.append(_ESCAPES.get(escaped, escaped))
                    pos += 1
            elif char == 0x28:
                depth += 1
                out.append(char)
            else:
                depth -= 1
                if depth == 0:
                    self.pos = pos
                    return bytes(out)
                out.append(char)

    def value(self, refs: bool = True) -> Any:
        token = self.token()
        if token is None:
            raise _End
        return self.build(token, refs)

    def build(self, token: tuple[str, Any], refs: bool, depth: int = 0) -> Any:
        kind, value = token
        if depth > 64:
            raise ValueError("nested too deeply")
        if kind == "value":
            if refs and type(value) is int:
                saved = self.pos
                generation, keyword = self.token(), self.token()
                if generation and type(generation[1]) is int and keyword == ("op", "R"):
                    return _Ref(value)
                self.pos = saved
            return value
        if kind == "aopen":
            items = []
            while (token := self.token()) is not None and token[0] != "aclose":
                items.append(self.build(token, refs, depth + 1))
            return items
        if kind == "dopen":
            entries: dict[str, Any] = {}
            while (token := self.token()) is not None and token[0] != "dclose":
                if token[0] == "value" and isinstance(token[1], _Name):
                    following = self.token()
                    if following is None or following[0] == "dclose":
                        break
                    entries[str(token[1])] = self.build(following, refs, depth + 1)
            return entries
        return value

    def skip_inline_image(self) -> None:
        match = _INLINE_END.search(self.data, self.pos)
        self.pos = match.end() if match else len(self.data)


_OBJECT = re.compile(rb"(?<!\d)(\d+)[\s\x00]+\d+[\s\x00]+obj\b")
_STREAM = re.compile(rb"[\s\x00]*stream\r?\n?")
_ENDSTREAM = re.compile(rb"[\s\x00]*endstream")
_ROOT = re.compile(rb"/Root[\s\x00]+(\d+)[\s\x00]+\d+[\s\x00]+R")
_ENCRYPT = re.compile(rb"/Encrypt[\s\x00]*(?:\d+[\s\x00]+\d+[\s\x00]+R|<<)")

# Glyph names a font's /Differences may give codes, for fonts without a ToUnicode map (a LaTeX PDF's).
_GLYPHS = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#", "dollar": "$", "percent": "%",
    "ampersand": "&", "quoteright": "’", "quotesingle": "'", "parenleft": "(", "parenright": ")",
    "asterisk": "*", "plus": "+", "comma": ",", "hyphen": "-", "minus": "−", "period": ".", "slash": "/",
    "colon": ":", "semicolon": ";", "less": "<", "equal": "=", "greater": ">", "question": "?", "at": "@",
    "bracketleft": "[", "backslash": "\\", "bracketright": "]", "underscore": "_", "quoteleft": "‘",
    "braceleft": "{", "bar": "|", "braceright": "}", "asciitilde": "~", "bullet": "•", "endash": "–",
    "emdash": "—", "quotedblleft": "“", "quotedblright": "”", "fi": "fi", "fl": "fl", "ff": "ff",
    "ffi": "ffi", "ffl": "ffl", "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "dotlessi": "ı", "section": "§",
    "paragraph": "¶", "degree": "°", "copyright": "©", "registered": "®",
}  # fmt: skip
_CP1252 = [bytes([code]).decode("cp1252", errors="replace") for code in range(256)]


def _glyph(name: str) -> str:
    if name in _GLYPHS:
        return _GLYPHS[name]
    if len(name) == 1:
        return name
    if match := re.fullmatch(r"uni([0-9A-Fa-f]{4})", name):
        return chr(int(match.group(1), 16))
    return ""


@dataclass
class _Font:
    cmap: dict[int, str] = field(default_factory=dict)
    width: int = 1  # bytes per character code
    simple: bool = True  # codes the map doesn't know are read as Windows-1252
    differences: dict[int, str] = field(default_factory=dict)


def _hex_bytes(digits: bytes) -> bytes:
    digits = re.sub(rb"\s", b"", digits)
    return bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode("ascii"))


def _utf16(raw: bytes) -> str:
    if len(raw) % 2:
        raw = b"\x00" + raw
    return raw.decode("utf-16-be", errors="replace")


def parse_cmap(data: bytes) -> tuple[dict[int, str], int | None]:
    """A ToUnicode map: character code to text, and the code width in bytes its codespace declares (if it does)."""
    cmap: dict[int, str] = {}
    width = None
    for block in re.finditer(rb"begincodespacerange(.*?)endcodespacerange", data, re.S):
        if found := re.findall(rb"<([0-9A-Fa-f\s]*)>", block.group(1)):
            width = max(1, len(re.sub(rb"\s", b"", found[0])) // 2)
    for block in re.finditer(rb"beginbfchar(.*?)endbfchar", data, re.S):
        found = re.findall(rb"<([0-9A-Fa-f\s]*)>", block.group(1))
        for source, target in zip(found[0::2], found[1::2], strict=False):
            cmap[int.from_bytes(_hex_bytes(source), "big")] = _utf16(_hex_bytes(target))
    for block in re.finditer(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for match in re.finditer(
            rb"<([0-9A-Fa-f\s]*)>\s*<([0-9A-Fa-f\s]*)>\s*(?:<([0-9A-Fa-f\s]*)>|\[(.*?)\])", block.group(1), re.S
        ):
            low = int.from_bytes(_hex_bytes(match.group(1)), "big")
            high = int.from_bytes(_hex_bytes(match.group(2)), "big")
            if high < low or high - low > 0xFFFF:
                continue
            if match.group(3) is not None:
                target = _hex_bytes(match.group(3))
                if len(target) % 2:
                    target = b"\x00" + target
                if not target:
                    continue
                prefix, last = target[:-2], int.from_bytes(target[-2:], "big")
                for offset in range(min(high - low, 0xFFFF - last) + 1):
                    cmap[low + offset] = _utf16(prefix + (last + offset).to_bytes(2, "big"))
            else:
                targets = re.findall(rb"<([0-9A-Fa-f\s]*)>", match.group(4))
                for code, target in zip(range(low, high + 1), targets, strict=False):
                    cmap[code] = _utf16(_hex_bytes(target))
    return cmap, width


def _inflate(raw: bytes) -> bytes:
    try:
        return zlib.decompressobj().decompress(raw, MAX_INFLATED)
    except zlib.error:
        try:
            return zlib.decompressobj(-zlib.MAX_WBITS).decompress(raw[2:], MAX_INFLATED)
        except zlib.error:
            return b""


class _Pdf:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.objects: dict[int, tuple[Any, bytes | None]] = {}
        self._fonts: dict[int, _Font] = {}
        self.unmapped = False  # text was shown in a font whose codes can't be turned back into characters
        self._scan()
        self._unpack_object_streams()

    def _scan(self) -> None:
        data, pos = self.data, 0
        while len(self.objects) < MAX_PDF_OBJECTS and (match := _OBJECT.search(data, pos)):
            lexer = _Lexer(data, match.end())
            try:
                value = lexer.value()
            except (_End, ValueError, RecursionError):
                pos = match.end()
                continue
            stream = None
            pos = lexer.pos
            if isinstance(value, dict) and (start := _STREAM.match(data, lexer.pos)):
                begin = start.end()
                length = value.get("Length")
                end = -1
                if type(length) is int and 0 <= length and _ENDSTREAM.match(data, begin + length):
                    end = begin + length
                else:
                    found = data.find(b"endstream", begin)
                    end = len(data) if found < 0 else found
                    while end > begin and data[end - 1] in b"\r\n":
                        end -= 1
                stream = data[begin:end]
                pos = end
            self.objects[int(match.group(1))] = (value, stream)

    def _unpack_object_streams(self) -> None:
        for value, stream in list(self.objects.values()):
            if not (isinstance(value, dict) and value.get("Type") == "ObjStm" and stream is not None):
                continue
            raw = self.decode(value, stream)
            first = value.get("First")
            if raw is None or type(first) is not int:
                continue
            numbers = [int(n) for n in re.findall(rb"\d+", raw[:first])]
            for number, offset in zip(numbers[0::2], numbers[1::2], strict=False):
                if self.objects.get(number, (None,))[0] is not None or len(self.objects) >= MAX_PDF_OBJECTS:
                    continue
                try:
                    self.objects[number] = (_Lexer(raw, first + offset).value(), None)
                except (_End, ValueError, RecursionError):
                    continue

    def get(self, value: Any) -> Any:
        for _ in range(32):
            if not isinstance(value, _Ref):
                return value
            value = self.objects.get(value.number, (None, None))[0]
        return None

    def decode(self, info: dict[str, Any], raw: bytes) -> bytes | None:
        """A stream's data with its filters undone, or None when it's an image or in a form that isn't read here."""
        filters = self.get(info.get("Filter"))
        for name in filters if isinstance(filters, list) else [filters] if filters else []:
            name = self.get(name)
            if name in ("FlateDecode", "Fl"):
                raw = _inflate(raw)
            elif name in ("ASCIIHexDecode", "AHx"):
                raw = _hex_bytes(re.sub(rb"[^0-9A-Fa-f]", b"", raw.split(b">")[0]))
            elif name in ("ASCII85Decode", "A85"):
                body = raw.strip().removeprefix(b"<~").split(b"~>")[0]
                raw = base64.a85decode(re.sub(rb"\s", b"", body))
            else:
                return None
        return raw

    def stream(self, ref: Any) -> bytes:
        if not isinstance(ref, _Ref):
            return b""
        info, raw = self.objects.get(ref.number, (None, None))
        if not isinstance(info, dict) or raw is None:
            return b""
        return self.decode(info, raw) or b""

    def pages(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Each page and the resources it uses (inherited from the page tree when the page has none of its own)."""
        found: list[tuple[dict[str, Any], dict[str, Any]]] = []
        seen: set[int] = set()

        def walk(node: Any, inherited: dict[str, Any], depth: int) -> None:
            if isinstance(node, _Ref):
                if node.number in seen:
                    return
                seen.add(node.number)
            node = self.get(node)
            if not isinstance(node, dict) or depth > 64 or len(found) >= MAX_PDF_PAGES:
                return
            resources = self.get(node.get("Resources"))
            resources = resources if isinstance(resources, dict) else inherited
            kids = self.get(node.get("Kids"))
            if isinstance(kids, list) and node.get("Type") != "Page":
                for kid in kids:
                    walk(kid, resources, depth + 1)
            else:
                found.append((node, resources))

        roots = _ROOT.findall(self.data)
        catalog = self.get(_Ref(int(roots[-1]))) if roots else None
        if not isinstance(catalog, dict):
            catalog = next(
                (v for v, _ in self.objects.values() if isinstance(v, dict) and v.get("Type") == "Catalog"), None
            )
        if isinstance(catalog, dict):
            walk(catalog.get("Pages"), {}, 0)
        if not found:
            for number in sorted(self.objects):
                value = self.objects[number][0]
                if isinstance(value, dict) and value.get("Type") == "Page" and len(found) < MAX_PDF_PAGES:
                    resources = self.get(value.get("Resources"))
                    found.append((value, resources if isinstance(resources, dict) else {}))
        return found

    def has_images(self) -> bool:
        return any(isinstance(v, dict) and v.get("Subtype") == "Image" for v, _ in self.objects.values())

    def font(self, ref: Any) -> _Font:
        key = ref.number if isinstance(ref, _Ref) else None
        if key is not None and key in self._fonts:
            return self._fonts[key]
        info = self.get(ref)
        info = info if isinstance(info, dict) else {}
        composite = info.get("Subtype") == "Type0"
        font = _Font(width=2 if composite else 1, simple=not composite)
        if (unicode := info.get("ToUnicode")) is not None:
            cmap, width = parse_cmap(self.stream(unicode))
            font.cmap = cmap
            font.width = width or font.width
        encoding = self.get(info.get("Encoding"))
        if isinstance(encoding, dict) and isinstance(differences := self.get(encoding.get("Differences")), list):
            code = 0
            for entry in differences:
                if type(entry) is int:
                    code = entry
                elif isinstance(entry, _Name):
                    if glyph := _glyph(str(entry)):
                        font.differences[code] = glyph
                    code += 1
        if key is not None:
            self._fonts[key] = font
        return font

    def show(self, font: _Font | None, raw: bytes) -> str:
        if font is None:
            return "".join(_CP1252[b] for b in raw)
        if font.width == 2:
            if not font.cmap:
                self.unmapped = True
                return ""
            return "".join(font.cmap.get(raw[i] << 8 | raw[i + 1], "") for i in range(0, len(raw) - 1, 2))
        out = []
        for code in raw:
            if code in font.cmap:
                out.append(font.cmap[code])
            elif code in font.differences:
                out.append(font.differences[code])
            elif font.simple:
                out.append(_CP1252[code])
        return "".join(out)

    def page_text(self, page: dict[str, Any], resources: dict[str, Any]) -> str:
        contents = self.get(page.get("Contents"))
        refs = contents if isinstance(contents, list) else [page.get("Contents")]
        data = b"\n".join(self.stream(ref) for ref in refs)
        declared = self.get(resources.get("Font"))
        fonts = {str(n): self.font(r) for n, r in declared.items()} if isinstance(declared, dict) else {}

        out: list[str] = []
        operands: list[Any] = []
        font: _Font | None = None
        last_y: float | None = None

        def newline() -> None:
            if out and not out[-1].endswith("\n"):
                out.append("\n")

        def space() -> None:
            if out and out[-1][-1:] not in (" ", "\n"):
                out.append(" ")

        lexer = _Lexer(data)
        while (token := lexer.token()) is not None:
            if token[0] != "op":
                operands.append(lexer.build(token, refs=False))
                continue
            op = token[1]
            if op == "Tf" and len(operands) >= 2:
                font = fonts.get(str(operands[-2]))
            elif op == "Tj" and operands and isinstance(operands[-1], bytes):
                out.append(self.show(font, operands[-1]))
            elif op in ("'", '"') and operands and isinstance(operands[-1], bytes):
                newline()
                out.append(self.show(font, operands[-1]))
            elif op == "TJ" and operands and isinstance(operands[-1], list):
                for part in operands[-1]:
                    if isinstance(part, bytes):
                        out.append(self.show(font, part))
                    elif isinstance(part, int | float) and part < -250:
                        space()
            elif op in ("Td", "TD") and len(operands) >= 2 and _numeric(operands[-1], operands[-2]):
                if abs(operands[-1]) > 0.01:
                    newline()
                elif operands[-2] > 0:
                    space()
            elif op == "Tm" and len(operands) >= 6 and _numeric(operands[-1]):
                y = float(operands[-1])
                if last_y is not None and abs(y - last_y) > 0.5:
                    newline()
                else:
                    space()
                last_y = y
            elif op == "T*":
                newline()
            elif op == "ID":
                lexer.skip_inline_image()
            operands.clear()
            if len(out) > 400_000:
                break
        return "".join(out)


def _numeric(*values: Any) -> bool:
    return all(isinstance(v, int | float) and not isinstance(v, bool) for v in values)


def _readable(text: str) -> bool:
    """Whether text looks like words rather than codes read with the wrong map (boxes, private-use glyphs)."""
    letters = [c for c in text if not c.isspace()]
    if not letters:
        return False
    bad = sum(1 for c in letters if c == "�" or "" <= c <= "" or not c.isprintable())
    return bad <= len(letters) * 0.15


def pdf_text(data: bytes) -> Extracted:
    """A PDF's text, page by page ("[Page 2]"), or why there's none."""
    if b"%PDF-" not in data[:1024]:
        return Extracted("", "It isn't really a PDF, so its text couldn't be read.")
    if _ENCRYPT.search(data):
        return Extracted(
            "", "It's a protected (encrypted) PDF, so its text can't be read here; it opens fine in a PDF viewer."
        )
    pdf = _Pdf(data)
    pages = pdf.pages()
    if not pages:
        return Extracted("", "It looks damaged: no pages were found in it.")
    parts, texts = [], []
    size = 0
    for number, (page, resources) in enumerate(pages, 1):
        before, pdf.unmapped = pdf.unmapped, False
        text = tidy(pdf.page_text(page, resources))
        unmapped, pdf.unmapped = pdf.unmapped, before or pdf.unmapped
        if text:
            parts.append(f"[Page {number}]\n{text}")
            texts.append(text)
            size += len(text)
        elif unmapped:  # text there, in a form that can't be turned back into words
            parts.append(f"[Page {number}: its text can't be read here]")
        else:  # a slide that's a picture: said where it is, so it can be looked at instead (Canvas's page=)
            parts.append(f"[Page {number}: no text, likely a picture or diagram]")
        if size > MAX_TEXT:
            break
    body = "\n\n".join(parts) if size else ""
    if not body:
        if pdf.unmapped:
            return Extracted("", "Its text is stored in a way that can't be turned back into words here.")
        if pdf.has_images():
            return Extracted("", "It's a scanned PDF: its pages are pictures, with no text that can be read from them.")
        return Extracted("", "The PDF has no text in it.")
    if not _readable("\n\n".join(texts)):  # the text itself, not the page markers
        return Extracted("", "Its text is stored in a way that can't be turned back into words here.")
    return Extracted(body)
