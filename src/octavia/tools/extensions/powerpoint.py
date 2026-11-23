"""The user's PowerPoint files, edited as files rather than by clicking in PowerPoint: ``powerpoint_outline`` lists
a presentation's slides and their shapes (id, name, kind, position, size, text), ``edit_powerpoint`` replaces a
shape's text, fits text to its box, moves and resizes shapes and deletes them.

Only the standard library: python-pptx isn't a dependency, and a .pptx is a zip of XML parts. Only the slides that
change are rewritten; every other part is copied as it is. ElementTree renames namespace prefixes and leaves out
declarations no element uses, while ``mc:Ignorable`` and ``mc:Choice Requires`` name prefixes in attribute text, so
every prefix a slide declared is kept and declared again on its root: without that PowerPoint calls the file
damaged. The new file is written beside the old one and swapped in only once it's complete, so a failed edit leaves
the file as it was, and the edits of one call are all made or none is.

Replaced text keeps its formatting character by character: what the new text has in common with the old at its
start and end keeps the formatting it had (a bold "Goal:" label stays bold), and what's new takes the formatting of
the text it replaces. Each line is a paragraph and keeps the old paragraph's bullets and indent level; added lines
are formatted like the last one. Whether text fits its box is an estimate from its length and font size (the fonts'
real widths aren't known here), which is how much it's shrunk (``normAutofit fontScale``) or how tall its box is
made.

A file open in PowerPoint isn't edited: PowerPoint keeps its own copy and writes it back when it saves, which would
undo these edits, or these would replace changes the user hasn't saved yet. The user is asked to save and close it
first (PowerPoint's lock file, ``~$name.pptx``, says it's open, as does Windows refusing to let it be written). Only
files in the user's own folders are read or written, and nothing leaves this PC.
"""

from __future__ import annotations

import asyncio
import copy
import io
import os
import posixpath
import re
import shutil
import sys
import tempfile
import threading
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, NamedTuple, Self
from xml.etree import ElementTree
from xml.etree.ElementTree import Element

from pydantic import Field, model_validator

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions.files import FILES, FileMemory, KnownFolders, Refused, check_openable, known_folder

P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

EMU_PER_INCH = 914_400
EMU_PER_PT = 12_700
MAX_PART = 40_000_000  # bytes an XML part may inflate to
MAX_OUTLINE = 20_000  # characters of outline in one answer
MAX_EDITS = 30
CHAR_EM = 0.5  # a proportional font's average character width, as a share of its size
LINE_EM = 1.2  # a line's height, as a share of its font size
# The steps PowerPoint itself shrinks text by.
SCALES = (1.0, 0.925, 0.85, 0.775, 0.7, 0.625, 0.55, 0.475, 0.4, 0.325, 0.25)

_SHAPE_TAGS = {f"{P}sp", f"{P}pic", f"{P}graphicFrame", f"{P}grpSp", f"{P}cxnSp"}
_RUN_TAGS = {f"{A}r", f"{A}fld", f"{A}br"}
_FIT_TAGS = {f"{A}noAutofit", f"{A}normAutofit", f"{A}spAutoFit"}
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f]")  # not allowed in XML, or not meaningful in a slide's text
_SERIALIZE = threading.Lock()  # ElementTree's prefixes are module-wide


class Box(NamedTuple):
    """Where a shape is on its slide, in EMU."""

    x: int
    y: int
    cx: int
    cy: int


class EditError(Exception):
    """An edit that can't be made, and why (said to the model)."""


# --------------------------------------------------------------------------- reading


def _read(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > MAX_PART:
        raise ValueError(f"{name} is too big")
    return archive.read(info)


def _parse(data: bytes) -> tuple[Element, dict[str, str]]:
    """A part's root and every namespace prefix it declares, anywhere in it."""
    namespaces: dict[str, str] = {}
    events = ElementTree.iterparse(io.BytesIO(data), events=("start-ns",))
    for _, (prefix, uri) in events:
        namespaces.setdefault(prefix, uri)
    return events.root, namespaces  # type: ignore[attr-defined]


def _serialize(root: Element, namespaces: dict[str, str]) -> bytes:
    """A part's XML, with the prefixes it was written with and all of its namespace declarations."""
    with _SERIALIZE:
        for prefix, uri in namespaces.items():
            if prefix and not re.fullmatch(r"ns\d+", prefix):
                ElementTree.register_namespace(prefix, uri)
        body = ElementTree.tostring(root, encoding="unicode")
    end = body.index(">")
    if body[end - 1] == "/":
        end -= 1
    declared = set(re.findall(r'\sxmlns:([^=\s]+)="', body[:end]))
    missing = "".join(
        f' xmlns:{prefix}="{uri}"'
        for prefix, uri in namespaces.items()
        if prefix and prefix not in declared and not re.fullmatch(r"ns\d+", prefix)
    )
    body = body[:end] + missing + body[end:]
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n' + body.encode("utf-8")


def _rels(archive: zipfile.ZipFile, names: set[str], part: str) -> dict[str, tuple[str, str]]:
    """A part's relationships: id -> (type, the part it points to)."""
    folder, name = posixpath.split(part)
    rels_name = posixpath.join(folder, "_rels", f"{name}.rels")
    if rels_name not in names:
        return {}
    found = {}
    for rel in ElementTree.fromstring(_read(archive, rels_name)).iterfind(f"{REL}Relationship"):
        target = rel.get("Target", "")
        if rel.get("TargetMode") == "External" or not target:
            continue
        resolved = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
        found[rel.get("Id", "")] = (rel.get("Type", ""), resolved)
    return found


def _related(rels: dict[str, tuple[str, str]], kind: str) -> str | None:
    return next((target for rel_type, target in rels.values() if rel_type.endswith(f"/{kind}")), None)


def _placeholder(element: Element) -> Element | None:
    return element.find(f"*/{P}nvPr/{P}ph")


def _placeholder_type(ph: Element) -> str:
    kind = ph.get("type", "obj")
    return {"ctrTitle": "title", "subTitle": "body", "obj": "body"}.get(kind, kind)


def _own_xfrm(element: Element) -> Element | None:
    if element.tag == f"{P}graphicFrame":
        return element.find(f"{P}xfrm")
    holder = element.find(f"{P}grpSpPr" if element.tag == f"{P}grpSp" else f"{P}spPr")
    return None if holder is None else holder.find(f"{A}xfrm")


def _box_of(xfrm: Element | None) -> Box | None:
    if xfrm is None:
        return None
    off, ext = xfrm.find(f"{A}off"), xfrm.find(f"{A}ext")
    if off is None or ext is None:
        return None
    try:
        return Box(int(off.get("x", 0)), int(off.get("y", 0)), int(ext.get("cx", 0)), int(ext.get("cy", 0)))
    except ValueError:
        return None


@dataclass
class Layout:
    """Where a layout's (or master's) placeholders are: by index, and by type."""

    by_idx: dict[str, Box] = field(default_factory=dict)
    by_type: dict[str, Box] = field(default_factory=dict)

    @classmethod
    def of(cls, root: Element | None) -> Layout:
        layout = cls()
        if root is None:
            return layout
        for element in root.iter():
            if element.tag not in _SHAPE_TAGS or (ph := _placeholder(element)) is None:
                continue
            if (box := _box_of(_own_xfrm(element))) is not None:
                layout.by_idx.setdefault(ph.get("idx", "0"), box)
                layout.by_type.setdefault(_placeholder_type(ph), box)
        return layout


@dataclass
class Slide:
    number: int
    part: str
    root: Element
    namespaces: dict[str, str]
    layout: Layout
    master: Layout
    changed: bool = False

    @property
    def hidden(self) -> bool:
        return self.root.get("show") == "0"

    def tree(self) -> Element:
        tree = self.root.find(f"{P}cSld/{P}spTree")
        if tree is None:
            raise EditError(f"Slide {self.number} has no shapes.")
        return tree

    def shapes(self) -> list[Shape]:
        found: list[Shape] = []

        def visit(parent: Element, grouped: bool) -> None:
            for element in parent:
                if element.tag in _SHAPE_TAGS:
                    found.append(Shape(element, parent, grouped))
                    if element.tag == f"{P}grpSp":
                        visit(element, True)

        visit(self.tree(), False)
        return found

    def shape(self, shape_id: int) -> Shape:
        for shape in self.shapes():
            if shape.id == shape_id:
                return shape
        raise EditError(f"Slide {self.number} has no shape {shape_id}; call powerpoint_outline for the ids.")

    def box(self, shape: Shape) -> Box | None:
        """Where the shape is: its own position, or for a placeholder that has none, its layout's or master's."""
        if (box := _box_of(_own_xfrm(shape.element))) is not None:
            return box
        ph = _placeholder(shape.element)
        if ph is None:
            return None
        kind = _placeholder_type(ph)
        return (
            self.layout.by_idx.get(ph.get("idx", "0")) or self.layout.by_type.get(kind) or self.master.by_type.get(kind)
        )


@dataclass
class Shape:
    element: Element
    parent: Element
    grouped: bool

    @property
    def id(self) -> int:
        props = self.element.find(f"*/{P}cNvPr")
        try:
            return int(props.get("id", -1)) if props is not None else -1
        except ValueError:
            return -1

    @property
    def name(self) -> str:
        props = self.element.find(f"*/{P}cNvPr")
        return props.get("name", "") if props is not None else ""

    @property
    def kind(self) -> str:
        tag = self.element.tag
        if tag == f"{P}pic":
            return "picture"
        if tag == f"{P}grpSp":
            return "group"
        if tag == f"{P}cxnSp":
            return "line"
        if tag == f"{P}graphicFrame":
            data = self.element.find(f".//{A}graphicData")
            uri = data.get("uri", "") if data is not None else ""
            return "table" if uri.endswith("/table") else "chart" if "chart" in uri else "object"
        if (ph := _placeholder(self.element)) is not None:
            return f"{ph.get('type', 'content')} placeholder"
        props = self.element.find(f"{P}nvSpPr/{P}cNvSpPr")
        return "text box" if props is not None and props.get("txBox") == "1" else "shape"

    @property
    def body(self) -> Element | None:
        return self.element.find(f"{P}txBody") if self.element.tag == f"{P}sp" else None

    def paragraphs(self) -> list[Element]:
        body = self.body
        return [] if body is None else body.findall(f"{A}p")

    def default_pt(self) -> float:
        """The font size its text has when no size is written: what the default Office theme gives it."""
        ph = _placeholder(self.element)
        if ph is None:
            return 18.0
        return {"title": 44.0, "ctrTitle": 44.0, "subTitle": 24.0}.get(ph.get("type", "obj"), 28.0)


def _piece_text(piece: Element) -> str:
    if piece.tag == f"{A}br":
        return " "
    t = piece.find(f"{A}t")
    return (t.text or "") if t is not None else ""


def _paragraph_text(paragraph: Element) -> str:
    return "".join(_piece_text(child) for child in paragraph if child.tag in _RUN_TAGS)


class Deck:
    """A presentation, read: its slides in show order, parsed. Blocking."""

    def __init__(self, path: str) -> None:
        self.path = path
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            presentation, _ = _parse(_read(archive, "ppt/presentation.xml"))
            rels = _rels(archive, names, "ppt/presentation.xml")
            size = presentation.find(f"{P}sldSz")
            self.width = int(size.get("cx", 0)) if size is not None else 0
            self.height = int(size.get("cy", 0)) if size is not None else 0
            layouts: dict[str | None, Layout] = {None: Layout()}
            self.slides: list[Slide] = []
            for slide_id in presentation.iterfind(f"{P}sldIdLst/{P}sldId"):
                part = rels.get(slide_id.get(f"{R}id", ""), ("", ""))[1]
                if part not in names:
                    continue
                root, namespaces = _parse(_read(archive, part))
                layout_part = _related(_rels(archive, names, part), "slideLayout")
                master_part = _related(_rels(archive, names, layout_part), "slideMaster") if layout_part else None
                for each in (layout_part, master_part):
                    if each not in layouts:
                        layouts[each] = Layout.of(_parse(_read(archive, each))[0] if each in names else None)
                number = len(self.slides) + 1
                self.slides.append(Slide(number, part, root, namespaces, layouts[layout_part], layouts[master_part]))

    def slide(self, number: int) -> Slide:
        if not 1 <= number <= len(self.slides):
            raise EditError(f"There's no slide {number}; the presentation has {len(self.slides)}.")
        return self.slides[number - 1]

    def save(self) -> None:
        """Writes the changed slides into a new copy of the file beside it, then swaps it in."""
        changed = {slide.part: _serialize(slide.root, slide.namespaces) for slide in self.slides if slide.changed}
        if not changed:
            return
        folder = os.path.dirname(self.path)
        handle, temp = tempfile.mkstemp(prefix="~edit-", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(handle, "wb") as out:
                with zipfile.ZipFile(self.path) as source, zipfile.ZipFile(out, "w") as target:
                    for original in source.infolist():
                        info = copy.copy(original)
                        if info.filename in changed:
                            target.writestr(info, changed[info.filename])
                            continue
                        big = info.file_size >= zipfile.ZIP64_LIMIT
                        with source.open(original) as reading, target.open(info, "w", force_zip64=big) as writing:
                            shutil.copyfileobj(reading, writing)
            os.replace(temp, self.path)
        except BaseException:
            try:
                os.remove(temp)
            except OSError:
                pass
            raise


# --------------------------------------------------------------------------- fitting text


def _lines(text: str, per_line: int) -> int:
    """How many lines ``text`` wraps to, at ``per_line`` characters a line."""
    lines, used = 1, 0
    for word in text.split():
        if used == 0:
            used = len(word)
        elif used + 1 + len(word) <= per_line:
            used += 1 + len(word)
        else:
            lines, used = lines + 1, len(word)
        while used > per_line:
            lines, used = lines + 1, used - per_line
    return lines


def _size_pt(paragraph: Element, default: float) -> float:
    sizes = [rpr.get("sz") for rpr in paragraph.iter() if rpr.tag in (f"{A}rPr", f"{A}endParaRPr")]
    points = [int(sz) / 100 for sz in sizes if sz and sz.isdigit() and int(sz) > 0]
    return max(points) if points else default


def text_height_pt(paragraphs: Sequence[tuple[str, float]], width_pt: float, scale: float, wrap: bool = True) -> float:
    """About how tall text is, in points: each paragraph's (text, font size) wrapped to ``width_pt``."""
    total = 0.0
    for text, size in paragraphs:
        size *= scale
        per_line = max(1, int(width_pt / (size * CHAR_EM))) if wrap else sys.maxsize
        total += _lines(text, per_line) * size * LINE_EM
    return total


@dataclass(frozen=True)
class TextFrame:
    """A shape's text as fitting sees it: its paragraphs and the room inside its box, in points."""

    paragraphs: list[tuple[str, float]]
    width_pt: float
    height_pt: float
    insets_pt: float  # top and bottom margins together
    wrap: bool

    @classmethod
    def of(cls, shape: Shape, box: Box) -> TextFrame:
        body = shape.body
        body_pr = body.find(f"{A}bodyPr") if body is not None else None

        def inset(name: str, default: int) -> int:
            value = body_pr.get(name) if body_pr is not None else None
            return int(value) if value and value.lstrip("-").isdigit() else default

        left, right = inset("lIns", 91_440), inset("rIns", 91_440)
        top, bottom = inset("tIns", 45_720), inset("bIns", 45_720)
        default = shape.default_pt()
        paragraphs = [(_paragraph_text(p), _size_pt(p, default)) for p in shape.paragraphs()]
        return cls(
            paragraphs,
            width_pt=max(1.0, (box.cx - left - right) / EMU_PER_PT),
            height_pt=max(1.0, (box.cy - top - bottom) / EMU_PER_PT),
            insets_pt=(top + bottom) / EMU_PER_PT,
            wrap=body_pr is None or body_pr.get("wrap") != "none",
        )

    def height(self, scale: float = 1.0) -> float:
        return text_height_pt(self.paragraphs, self.width_pt, scale, self.wrap)

    def overflows(self) -> bool:
        return self.height() > self.height_pt


def _autofit(shape: Shape) -> str | None:
    """The shape's own auto-fit setting: "noAutofit", "normAutofit", "spAutoFit", or None."""
    body = shape.body
    body_pr = body.find(f"{A}bodyPr") if body is not None else None
    if body_pr is None:
        return None
    return next((child.tag.removeprefix(A) for child in body_pr if child.tag in _FIT_TAGS), None)


def _set_autofit(shape: Shape, tag: str, **attributes: str) -> None:
    body = shape.body
    assert body is not None
    body_pr = body.find(f"{A}bodyPr")
    if body_pr is None:
        body_pr = Element(f"{A}bodyPr")
        body.insert(0, body_pr)
    for old in [child for child in body_pr if child.tag in _FIT_TAGS]:
        body_pr.remove(old)
    at = 1 if body_pr.find(f"{A}prstTxWarp") is not None else 0
    body_pr.insert(at, Element(f"{A}{tag}", attributes))


Fit = Literal["auto", "shrink", "resize_shape", "off"]


def fit_text(slide: Slide, shape: Shape, mode: Fit, *, asked: bool) -> str:
    """Fits the shape's text to its box as ``mode`` says, and what was done ("" when nothing was). ``auto`` keeps
    the shape's own setting and shrinks text that would overflow; ``asked``: the user asked for fitting itself."""
    if shape.body is None:
        raise EditError(f"Shape {shape.id} ({shape.kind}) has no text to fit.")
    if mode == "off":
        _set_autofit(shape, "noAutofit")
        return "turned auto-fit off"
    box = slide.box(shape)
    if box is None:
        if mode == "resize_shape":
            raise EditError(f"I can't tell where shape {shape.id} is, so I can't size it to its text.")
        _set_autofit(shape, "normAutofit")
        return "turned on shrink-on-overflow; PowerPoint applies it the next time the text is edited there"
    frame = TextFrame.of(shape, box)
    if mode == "auto":
        current = _autofit(shape)
        if current == "spAutoFit":
            mode = "resize_shape"
        elif current == "normAutofit" or frame.overflows():
            mode = "shrink"
        else:
            return "it fits its box" if asked else ""
    if mode == "resize_shape":
        needed = round((frame.height() + frame.insets_pt) * EMU_PER_PT)
        _set_autofit(shape, "spAutoFit")
        if shape.grouped:
            return "set its box to grow with its text (PowerPoint resizes it the next time the text is edited)"
        _place(slide, shape, height=needed)
        return f"made its box {_inches(needed)} in tall to fit its text"
    scale = next((s for s in SCALES if frame.height(s) <= frame.height_pt), None)
    if scale is None:
        _set_autofit(shape, "normAutofit", fontScale=str(round(SCALES[-1] * 100_000)))
        return "shrank its text to 25%, and it may still not fit: shorten the text or enlarge the box"
    if scale == 1.0:
        _set_autofit(shape, "normAutofit")
        return "it fits its box (set to shrink if it grows)"
    _set_autofit(shape, "normAutofit", fontScale=str(round(scale * 100_000)))
    return f"shrank its text to about {round(scale * 100)}% so it fits its box"


# --------------------------------------------------------------------------- editing


def _retext(paragraph: Element, text: str) -> None:
    """Gives a paragraph new text, each character formatted like the one it replaces (see the module's docstring)."""
    pieces = [child for child in paragraph if child.tag in _RUN_TAGS]
    texts = [_piece_text(piece) for piece in pieces]
    old = "".join(texts)
    if text == old:
        return
    owner = [i for i, piece_text in enumerate(texts) for _ in piece_text]
    limit = min(len(old), len(text))
    start = 0
    while start < limit and old[start] == text[start]:
        start += 1
    end = 0
    while end < limit - start and old[-1 - end] == text[-1 - end]:
        end += 1
    if not owner:
        middle: int | None = None
    elif start == 0:
        middle = max(range(len(texts)), key=lambda i: len(texts[i]))  # the formatting most of the text has
    else:
        middle = owner[start] if start < len(old) else owner[-1]
    owners: list[int | None] = [*owner[:start], *[middle] * (len(text) - start - end), *owner[len(old) - end :]]
    runs: list[tuple[int | None, str]] = []
    for char, who in zip(text, owners, strict=True):
        if runs and runs[-1][0] == who:
            runs[-1] = (who, runs[-1][1] + char)
        else:
            runs.append((who, char))
    for piece in pieces:
        paragraph.remove(piece)
    end_pr = paragraph.find(f"{A}endParaRPr")
    at = list(paragraph).index(end_pr) if end_pr is not None else len(paragraph)
    for offset, (who, chunk) in enumerate(runs):
        run = Element(f"{A}r")
        source = pieces[who].find(f"{A}rPr") if who is not None else end_pr
        if source is not None:
            formatting = copy.deepcopy(source)
            formatting.tag = f"{A}rPr"
            for child in [c for c in formatting if c.tag == f"{A}extLst"]:
                formatting.remove(child)
            run.append(formatting)
        ElementTree.SubElement(run, f"{A}t").text = chunk
        paragraph.insert(at + offset, run)


def replace_text(shape: Shape, text: str) -> None:
    if shape.element.tag != f"{P}sp":
        raise EditError(f"Shape {shape.id} is a {shape.kind}; only text boxes, placeholders and shapes have text here.")
    body = shape.body
    if body is None:
        body = Element(f"{P}txBody")
        ElementTree.SubElement(body, f"{A}bodyPr")
        ElementTree.SubElement(body, f"{A}lstStyle")
        ElementTree.SubElement(body, f"{A}p")
        ext = shape.element.find(f"{P}extLst")
        at = list(shape.element).index(ext) if ext is not None else len(shape.element)
        shape.element.insert(at, body)
    lines = text.split("\n")
    paragraphs = body.findall(f"{A}p")
    if not paragraphs:
        paragraphs = [ElementTree.SubElement(body, f"{A}p")]
    for extra in paragraphs[len(lines) :]:
        body.remove(extra)
    last = paragraphs[min(len(lines), len(paragraphs)) - 1]
    for i, line in enumerate(lines):
        if i < len(paragraphs):
            _retext(paragraphs[i], line)
            continue
        added = copy.deepcopy(paragraphs[-1])
        body.insert(list(body).index(last) + 1, added)
        _retext(added, line)
        last = added


def _place(
    slide: Slide,
    shape: Shape,
    *,
    left: int | None = None,
    top: int | None = None,
    width: int | None = None,
    height: int | None = None,
) -> Box:
    """Moves or resizes a shape (EMU); a placeholder placed by its layout gets a position of its own first."""
    if shape.grouped:
        raise EditError(f"Shape {shape.id} is inside a group: move or resize the group instead.")
    box = slide.box(shape)
    if box is None:
        raise EditError(f"I can't tell where shape {shape.id} is, so I can't move or resize it.")
    xfrm = _own_xfrm(shape.element)
    if xfrm is None:
        if shape.element.tag == f"{P}graphicFrame":
            xfrm = Element(f"{P}xfrm")
            shape.element.insert(1, xfrm)
        else:
            holder = shape.element.find(f"{P}spPr")
            if holder is None:
                holder = Element(f"{P}spPr")
                shape.element.insert(1, holder)
            xfrm = Element(f"{A}xfrm")
            holder.insert(0, xfrm)
    off, ext = xfrm.find(f"{A}off"), xfrm.find(f"{A}ext")
    if off is None:
        off = Element(f"{A}off")
        xfrm.insert(0, off)
    if ext is None:
        ext = Element(f"{A}ext")
        xfrm.insert(1, ext)
    placed = Box(
        box.x if left is None else left,
        box.y if top is None else top,
        box.cx if width is None else width,
        box.cy if height is None else height,
    )
    off.set("x", str(placed.x))
    off.set("y", str(placed.y))
    ext.set("cx", str(placed.cx))
    ext.set("cy", str(placed.cy))
    return placed


def _inches(emu: int) -> str:
    return f"{emu / EMU_PER_INCH:.2f}".rstrip("0").rstrip(".")


def _emu(inches: float | None) -> int | None:
    return None if inches is None else round(inches * EMU_PER_INCH)


# --------------------------------------------------------------------------- which file


def busy_reason(path: str) -> str | None:
    """Why the file can't be written safely right now, or None. Blocking."""
    folder, name = os.path.split(path)
    if os.path.exists(os.path.join(folder, f"~${name}")):
        return "open"
    if not os.access(path, os.W_OK):
        return "read-only"
    if sys.platform == "win32":
        try:
            with open(path, "r+b"):  # nothing is written: this only asks whether another program holds it
                pass
        except PermissionError:
            return "locked"
        except OSError:
            pass
    return None


def busy_message(name: str, reason: str) -> str:
    if reason == "read-only":
        return f"{name} is read-only, so I can't edit it. Nothing was changed."
    what = "is open in PowerPoint" if reason == "open" else "is open in another program, probably PowerPoint"
    return (
        f"{name} {what}, so I haven't touched it: PowerPoint would write its own copy back when it saves, undoing my "
        "edits, or my edits would replace changes not saved yet. Ask the user to save and close it in PowerPoint "
        "(or offer to close PowerPoint for them with close_application, which lets them save), then try again; "
        "they can reopen it afterwards with open_file. Nothing was changed."
    )


class DeckArgs(ToolArgs):
    number: int | None = Field(default=None, ge=1, description="A find_files result number.")
    path: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Or a path, full or as find_files shows it.",
    )

    @model_validator(mode="after")
    def _one_file(self) -> Self:
        if (self.number is None) == (self.path is None):
            raise ValueError("give either a result number from find_files or a path, not both")
        return self


def _resolve(args: DeckArgs, folders: KnownFolders, memory: FileMemory) -> tuple[str, str]:
    """The presentation's path and name, once it's sure to be a .pptx of the user's own. Raises Refused. Blocking."""
    if args.number is not None:
        results = memory.results()
        if not results:
            raise Refused("I have no recent find_files results (they're kept for ten minutes). Search again first.")
        if args.number > len(results):
            raise Refused(f"There's no result {args.number}; pick 1 to {len(results)}.")
        target = results[args.number - 1].path
    else:
        assert args.path is not None
        target = args.path
    opening = check_openable(target, folders)
    extension = os.path.splitext(opening.name)[1].casefold()
    if opening.kind != "file" or extension != ".pptx":
        if extension == ".ppt":
            raise Refused(f"{opening.name} is an old .ppt file; it needs saving as .pptx in PowerPoint first.")
        raise Refused(f"{opening.name} isn't a PowerPoint (.pptx) file.")
    return opening.path, opening.name


def _load(path: str, name: str) -> Deck:
    try:
        return Deck(path)
    except (zipfile.BadZipFile, KeyError, ValueError, ElementTree.ParseError, OSError) as exc:
        raise Refused(f"I couldn't read {name} as a PowerPoint file ({exc}).") from None


# --------------------------------------------------------------------------- powerpoint_outline


class OutlineArgs(DeckArgs):
    slide: int | None = Field(default=None, ge=1, description="Only this slide.")


def _describe_shape(slide: Slide, shape: Shape) -> list[str]:
    box = slide.box(shape)
    where = f" at {_inches(box.x)}, {_inches(box.y)} in, {_inches(box.cx)} x {_inches(box.cy)} in" if box else ""
    notes = []
    if shape.grouped:
        notes.append("in a group")
    fit = _autofit(shape)
    if fit == "normAutofit":
        notes.append("shrinks text to fit")
    elif fit == "spAutoFit":
        notes.append("box grows with its text")
    elif (
        # a placeholder without a setting of its own usually shrinks its text, as its master says
        fit is None
        and box is not None
        and shape.body is not None
        and _placeholder(shape.element) is None
        and TextFrame.of(shape, box).overflows()
    ):
        notes.append("text looks too long for its box")
    extra = f" [{', '.join(notes)}]" if notes else ""
    lines = [f'- #{shape.id} "{shape.name}" ({shape.kind}){where}{extra}']
    for paragraph in shape.paragraphs():
        properties = paragraph.find(f"{A}pPr")
        level = properties.get("lvl", "0") if properties is not None else "0"
        indent = "    " * (int(level) if level.isdigit() else 0)
        lines.append(f"    {indent}| {_paragraph_text(paragraph)}")
    return lines


def outline(deck: Deck, name: str, only: int | None) -> str:
    size = f", {_inches(deck.width)} x {_inches(deck.height)} in" if deck.width and deck.height else ""
    count = len(deck.slides)
    lines = [f'{name}: {count} slide{"" if count == 1 else "s"}{size}. Each shape: #id "name" (kind), position, size.']
    for slide in [deck.slide(only)] if only is not None else deck.slides:
        lines.append(f"Slide {slide.number}{' (hidden)' if slide.hidden else ''}:")
        shapes = slide.shapes()
        if not shapes:
            lines.append("  (no shapes)")
        for shape in shapes:
            lines.extend(_describe_shape(slide, shape))
    text = "\n".join(lines)
    if len(text) > MAX_OUTLINE:
        text = text[:MAX_OUTLINE] + "\n(cut short: ask for one slide at a time to see the rest)"
    return text


class PowerPointOutline(Tool):
    name = "powerpoint_outline"
    description = (
        "List a .pptx's slides and shapes (id, position and size in inches, text). Use before edit_powerpoint."
    )
    Args = OutlineArgs
    risk = RiskLevel.SAFE  # reads the file only
    category = "files"
    timeout_s = 30.0

    def __init__(self, folders: KnownFolders | None = None, memory: FileMemory | None = None) -> None:
        self._folders = folders or known_folder
        self.memory = memory if memory is not None else FILES

    async def describe_call(self, args: OutlineArgs, ctx: ToolContext) -> str:
        return f"read the slides of {args.path or f'result {args.number}'}"

    def _outline(self, args: OutlineArgs) -> str:
        path, name = _resolve(args, self._folders, self.memory)
        deck = _load(path, name)
        try:
            text = outline(deck, name, args.slide)
        except EditError as exc:
            raise Refused(str(exc)) from None
        if busy_reason(path) == "open":
            text += (
                "\nNote: it's open in PowerPoint, so changes not saved there aren't in this list. Before editing it, "
                "the user needs to save and close it."
            )
        return text

    async def run(self, args: OutlineArgs, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult.success(await asyncio.to_thread(self._outline, args))
        except Refused as exc:
            return ToolResult.error(str(exc))


# --------------------------------------------------------------------------- edit_powerpoint


class SlideEdit(ToolArgs):
    slide: int = Field(ge=1, description="From powerpoint_outline.")
    shape_id: int = Field(ge=0, description="From powerpoint_outline.")
    action: Literal["replace_text", "fit_text", "move_resize", "delete"] = Field(
        description="move_resize sets any of left, top, width, height (inches)."
    )
    text: str | None = Field(
        default=None,
        max_length=10_000,
        description="All the new text, a line per paragraph, no bullet characters.",
    )
    fit: Fit = Field(default="auto", description="resize_shape grows the box; auto keeps the shape's own setting.")
    left: float | None = Field(default=None, ge=-100, le=100)
    top: float | None = Field(default=None, ge=-100, le=100)
    width: float | None = Field(default=None, gt=0, le=100)
    height: float | None = Field(default=None, gt=0, le=100)

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if self.action == "replace_text":
            if self.text is None:
                raise ValueError("replace_text needs text")
            self.text = _CONTROL.sub("", self.text.replace("\r\n", "\n").replace("\r", "\n"))
        elif self.text is not None:
            raise ValueError("text is only for replace_text")
        placing = (self.left, self.top, self.width, self.height)
        if self.action == "move_resize" and all(value is None for value in placing):
            raise ValueError("move_resize needs at least one of left, top, width, height")
        if self.action != "move_resize" and any(value is not None for value in placing):
            raise ValueError("left, top, width and height are only for move_resize")
        return self


class EditArgs(DeckArgs):
    edits: list[SlideEdit] = Field(min_length=1, max_length=MAX_EDITS, description="Applied in order; all or none.")


def _edit_phrase(edit: SlideEdit, label: str) -> str:
    where = f"on slide {edit.slide}"
    if edit.action == "replace_text":
        text = (edit.text or "").replace("\n", " / ")
        short = text if len(text) <= 60 else text[:57] + "..."
        return f'put "{short}" in {label} {where}'
    if edit.action == "fit_text":
        return {
            "shrink": f"shrink the text in {label} {where} to fit",
            "resize_shape": f"resize {label} {where} to fit its text",
            "off": f"turn off auto-fit for {label} {where}",
        }.get(edit.fit, f"fit the text in {label} {where}")
    if edit.action == "delete":
        return f"delete {label} from slide {edit.slide}"
    moving = edit.left is not None or edit.top is not None
    sizing = edit.width is not None or edit.height is not None
    verb = "move and resize" if moving and sizing else "move" if moving else "resize"
    return f"{verb} {label} {where}"


def apply_edit(deck: Deck, edit: SlideEdit) -> str:
    """Makes one edit in memory; what was done. Raises EditError."""
    slide = deck.slide(edit.slide)
    shape = slide.shape(edit.shape_id)
    label = f'"{shape.name}"' if shape.name else f"shape {shape.id}"
    done = f"{label} on slide {slide.number}"
    slide.changed = True
    if edit.action == "delete":
        shape.parent.remove(shape.element)
        return f"deleted {done}"
    if edit.action == "move_resize":
        placed = _place(
            slide, shape, left=_emu(edit.left), top=_emu(edit.top), width=_emu(edit.width), height=_emu(edit.height)
        )
        note = ""
        if deck.width and deck.height and (placed.x + placed.cx > deck.width or placed.y + placed.cy > deck.height):
            note = " (it now reaches past the slide's edge)"
        elif placed.x < 0 or placed.y < 0:
            note = " (it now reaches past the slide's edge)"
        where = f"at {_inches(placed.x)}, {_inches(placed.y)} in, {_inches(placed.cx)} x {_inches(placed.cy)} in"
        return f"placed {done} {where}{note}"
    if edit.action == "replace_text":
        replace_text(shape, edit.text or "")
        fitted = fit_text(slide, shape, edit.fit, asked=edit.fit != "auto")
        return f"replaced the text of {done}" + (f"; {fitted}" if fitted else "")
    return f"{done}: {fit_text(slide, shape, edit.fit, asked=True)}"


class EditPowerPoint(Tool):
    name = "edit_powerpoint"
    description = (
        "Edit a .pptx's shapes without PowerPoint, keeping formatting. Call powerpoint_outline first. Won't touch a "
        "file open in PowerPoint. Saves in place; never uploads or submits."
    )
    Args = EditArgs
    risk = RiskLevel.CONFIRM  # rewrites the user's file, and deleting a shape can't be undone from here
    category = "files"
    timeout_s = 60.0

    def __init__(
        self,
        folders: KnownFolders | None = None,
        memory: FileMemory | None = None,
        *,
        busy: Callable[[str], str | None] = busy_reason,
    ) -> None:
        self._folders = folders or known_folder
        self.memory = memory if memory is not None else FILES
        self._busy = busy

    def _labels(self, args: EditArgs) -> tuple[str, dict[tuple[int, int], str]]:
        """The file's name and the edited shapes' names, for the question the user is asked. Blocking."""
        path, name = _resolve(args, self._folders, self.memory)
        labels = {}
        try:
            deck = Deck(path)
            for edit in args.edits:
                shape = deck.slide(edit.slide).shape(edit.shape_id)
                if shape.name:
                    labels[(edit.slide, edit.shape_id)] = f'"{shape.name}"'
        except Exception:  # only the wording of the question depends on it
            pass
        return name, labels

    async def describe_call(self, args: EditArgs, ctx: ToolContext) -> str:
        try:
            name, labels = await asyncio.to_thread(self._labels, args)
        except Refused:
            name, labels = args.path or f"result {args.number}", {}
        phrases = [
            _edit_phrase(edit, labels.get((edit.slide, edit.shape_id), f"shape {edit.shape_id}")) for edit in args.edits
        ]
        shown = "; ".join(phrases[:4]) + (f"; and {len(phrases) - 4} more" if len(phrases) > 4 else "")
        return f"edit {name}: {shown}"

    async def precheck(self, args: EditArgs, ctx: ToolContext) -> str | None:
        def check() -> str | None:
            try:
                path, name = _resolve(args, self._folders, self.memory)
            except Refused as exc:
                return str(exc)
            reason = self._busy(path)
            return busy_message(name, reason) if reason else None

        return await asyncio.to_thread(check)

    def _edit(self, args: EditArgs) -> ToolResult:
        try:
            path, name = _resolve(args, self._folders, self.memory)
        except Refused as exc:
            return ToolResult.error(str(exc))
        if reason := self._busy(path):
            return ToolResult.error(busy_message(name, reason))
        try:
            deck = _load(path, name)
        except Refused as exc:
            return ToolResult.error(str(exc))
        done = []
        for number, edit in enumerate(args.edits, 1):
            try:
                done.append(apply_edit(deck, edit))
            except EditError as exc:
                return ToolResult.error(f"Edit {number} can't be made: {exc} Nothing was changed.")
        if reason := self._busy(path):  # opened while the edits were being made
            return ToolResult.error(busy_message(name, reason))
        try:
            deck.save()
        except PermissionError:
            return ToolResult.error(busy_message(name, "locked"))
        except OSError as exc:
            return ToolResult.error(f"I couldn't save {name} ({exc.strerror or exc}). Nothing was changed.")
        lines = [f"Saved {name}:", *(f"- {line}" for line in done)]
        lines.append("Text fitting is estimated from text length; open_file shows the result in PowerPoint.")
        return ToolResult.success("\n".join(lines), data={"edits": len(done)})

    async def run(self, args: EditArgs, ctx: ToolContext) -> ToolResult:
        return await asyncio.to_thread(self._edit, args)


TOOLS = [PowerPointOutline, EditPowerPoint]
