"""New Office files made from scratch: ``create_word_document``, ``create_powerpoint`` and ``create_excel_workbook``
write a .docx, .pptx or .xlsx into the user's Documents folder, or another folder of their own.

Like the tools that edit those files, only the standard library is used: an Office file is a zip of XML parts, and the
few parts a plain new file needs are written here, with Office's own defaults (Calibri, the Office theme and colours)
so it looks like one the app made. word_outline, powerpoint_outline and excel_outline read these files, so the edit
tools can change them afterwards.

A new file never replaces one. When its name is taken, " (2)", " (3)" and so on is added, as File Explorer does, and
the file is only created if nothing has that name yet (``open(..., "xb")``), so even a file made at the same moment
isn't overwritten. The folder must be in the user's own folders, judged as open_file judges a path: as written, before
anything on disk is touched, and again once links are followed. A folder that's missing there is made.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import math
import os
import re
import zipfile
from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal, Self
from xml.sax.saxutils import escape

from pydantic import Field, model_validator

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions._office import clean_text
from octavia.tools.extensions.excel import MAX_FORMULA, MAX_TEXT, cell_name, column_letters, typed
from octavia.tools.extensions.files import (
    _DRIVE,
    _SKIP_FOLDERS,
    LABELS,
    KnownFolders,
    Refused,
    _inside,
    _written_path,
    known_folder,
    user_places,
)

MAX_STEM = 120  # characters of a new file's name
MAX_COPIES = 999  # "name (999).docx" is the last name tried
_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_SEPARATORS = re.compile(r"[\\/]+")
_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))}
)
_SHEET_NAME_BAD = re.compile(r"[\[\]:*?/\\]")

HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CT = "application/vnd.openxmlformats-officedocument"
W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
PML_NS = (
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    f'xmlns:r="{REL}" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
)
S_NS = f'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="{REL}"'

Value = str | int | float | bool | None


def _text(value: Value) -> str:
    """A cell's value as it'd be typed: numbers as written, TRUE/FALSE, nothing for None."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and math.isfinite(value) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return clean_text(str(value))


def _attr(text: str) -> str:
    return escape(text, {'"': "&quot;"})


# --------------------------------------------------------------------------- where the file goes


@dataclass(frozen=True)
class Destination:
    """The folder a new file goes in: where it is once links are followed (made when it's missing), and how it's
    shown ("Documents\\School")."""

    path: str
    shown: str
    missing: bool


def destination(folder: str | None, folders: KnownFolders) -> Destination:
    """Where a new file named for ``folder`` goes: Documents when none is named, and a folder named alone ("School")
    inside Documents. Raises Refused when it isn't one of the user's own folders. Blocking: it looks on disk, but
    changes nothing."""
    text = (folder or "").strip().strip('"').strip() or LABELS["documents"]
    path = Path(os.path.expanduser(text))
    if not path.is_absolute() and not text.startswith("~"):
        first = path.parts[0].casefold() if path.parts else ""
        if first not in {label.casefold() for label in LABELS.values()}:
            text = os.path.join(LABELS["documents"], text)
    written = _written_path(text, folders)
    drive = os.path.splitdrive(written)[0]
    if not _DRIVE.fullmatch(drive) or ":" in written[len(drive) :]:
        raise Refused("I only save files on this PC's own drives, in your folders.")
    places = user_places(folders)
    place = next((p for p in places if _inside(written, p.path) or _inside(written, p.real)), None)
    if place is None:
        raise Refused("That's outside your own folders, so I won't save a file there.")
    base = place.path if _inside(written, place.path) else place.real
    relative = os.path.relpath(written, base)
    parts = [] if relative == "." else relative.split(os.sep)
    if any(part.startswith((".", "~$")) or part.casefold() in _SKIP_FOLDERS for part in parts):
        raise Refused("That's a hidden or program folder, so I won't save a file there.")
    existing = written
    while not os.path.lexists(existing):
        parent = os.path.dirname(existing)
        if parent == existing:
            break
        existing = parent
    real = os.path.realpath(existing)
    # Again, where it really is: a link or junction inside the user's folders may lead anywhere.
    if not any(_inside(real, p.real) for p in places):
        raise Refused("That leads outside your own folders, so I won't save a file there.")
    if not os.path.isdir(real):
        raise Refused(f"{os.path.basename(existing)} is a file, not a folder, so I can't save a file in it.")
    rest = os.path.relpath(written, existing)
    target = real if rest == "." else os.path.join(real, rest)
    shown = place.label if not parts else "\\".join((place.label, *parts))
    return Destination(target, shown, missing=rest != ".")


def file_stem(name: str, extension: str, fallback: str) -> str:
    """A name Windows takes for the new file, without its extension: slashes become dashes, and what a file name
    can't hold is left out."""
    stem = name.strip()
    if stem.casefold().endswith(extension):
        stem = stem[: -len(extension)]
    stem = _SEPARATORS.sub("-", stem)
    stem = " ".join(_BAD_CHARS.sub(" ", stem).split())
    stem = stem.removeprefix("~$")[:MAX_STEM].strip(" .")
    if not stem or stem.split(".")[0].strip().casefold() in _RESERVED:
        return fallback
    return stem


def write_new(folder: str, stem: str, extension: str, data: bytes) -> str:
    """Writes ``data`` as a new file in ``folder``, named ``stem``, or when that's taken ``stem (2)`` and on; the name
    it got. Never replaces anything. Blocking."""
    for number in range(1, MAX_COPIES + 1):
        name = f"{stem}{extension}" if number == 1 else f"{stem} ({number}){extension}"
        path = os.path.join(folder, name)
        if os.path.lexists(path):
            continue
        try:
            out = open(path, "xb")
        except FileExistsError:
            continue
        try:
            with out:
                out.write(data)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(path)
            raise
        return name
    raise Refused(f"There are already too many files called {stem} there; pick another name.")


def _package(parts: dict[str, str]) -> bytes:
    """A zip of XML parts, as Office writes them: the content types first."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts.items():
            archive.writestr(name, text.encode("utf-8"))
    return buffer.getvalue()


def _content_types(overrides: dict[str, str]) -> str:
    items = "".join(f'<Override PartName="/{part}" ContentType="{kind}"/>' for part, kind in overrides.items())
    return (
        f'{HEAD}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        f'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        f'<Default Extension="xml" ContentType="application/xml"/>{items}</Types>'
    )


def _relationships(*items: tuple[str, str, str]) -> str:
    """A part's relationships: (id, type, target)."""
    rels = "".join(f'<Relationship Id="{i}" Type="{REL}/{kind}" Target="{target}"/>' for i, kind, target in items)
    return f'{HEAD}<Relationships xmlns="{PKG_REL}">{rels}</Relationships>'


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


# --------------------------------------------------------------------------- the tools' common ground


class NewFileArgs(ToolArgs):
    name: str = Field(
        min_length=1, max_length=200, description="The new file's name, e.g. 'Lab report' (the extension is added)."
    )
    folder: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Where to save it, in the user's own folders: e.g. 'Desktop', 'Documents\\School', or a folder name alone "
            "('School'), which is taken inside Documents. A missing folder is made. Default: Documents."
        ),
    )


class CreateOfficeFile(Tool):
    """Writes a new Office file from what ``build`` makes of the arguments."""

    extension: ClassVar[str]
    kind: ClassVar[str]  # "Word document": what the user is asked about
    fallback: ClassVar[str]  # the name when the one given can't be used
    tools: ClassVar[str]  # the tools that read and edit the file afterwards
    risk = RiskLevel.CONFIRM  # writes a new file into the user's folders (never over an existing one)
    category = "files"
    timeout_s = 30.0

    def __init__(self, folders: KnownFolders | None = None) -> None:
        self._folders = folders or known_folder

    @abstractmethod
    def build(self, args: Any) -> tuple[bytes, str]:
        """The file's bytes, and what's in it in a few words ("a title, 2 headings and a table")."""

    async def describe_call(self, args: NewFileArgs, ctx: ToolContext) -> str:
        stem = file_stem(args.name, self.extension, self.fallback)
        try:
            where = (await asyncio.to_thread(destination, args.folder, self._folders)).shown
        except Refused:
            where = args.folder or "Documents"
        return f'create the {self.kind} "{stem}{self.extension}" in {where}'

    async def precheck(self, args: NewFileArgs, ctx: ToolContext) -> str | None:
        try:
            await asyncio.to_thread(destination, args.folder, self._folders)
        except Refused as exc:
            return str(exc)
        return None

    def _create(self, args: NewFileArgs) -> ToolResult:
        try:
            place = destination(args.folder, self._folders)
        except Refused as exc:
            return ToolResult.error(str(exc))
        stem = file_stem(args.name, self.extension, self.fallback)
        try:
            data, contents = self.build(args)
            if place.missing:
                os.makedirs(place.path, exist_ok=True)
            name = write_new(place.path, stem, self.extension, data)
        except Refused as exc:
            return ToolResult.error(str(exc))
        except OSError as exc:
            return ToolResult.error(f"I couldn't save the file in {place.shown} ({exc.strerror or exc}).")
        shown = f"{place.shown}\\{name}"
        lines = [f"Created {name} in {place.shown}, with {contents}."]
        wanted = f"{stem}{self.extension}"
        if name != wanted:
            lines.append(f"{wanted} already existed there, so it wasn't touched and the new file is named {name}.")
        lines.append(f"To open it, call open_file with path '{shown}'; to change it, {self.tools} with that path.")
        return ToolResult.success(
            "\n".join(lines), data={"path": os.path.join(place.path, name), "renamed": name != wanted}
        )

    async def run(self, args: NewFileArgs, ctx: ToolContext) -> ToolResult:
        return await asyncio.to_thread(self._create, args)


# --------------------------------------------------------------------------- Word

TEXT_WIDTH = 9360  # twips between the margins of a Letter page with 1" margins

_WORD_STYLES = (
    f"{HEAD}<w:styles {W_NS}>"
    "<w:docDefaults><w:rPrDefault><w:rPr>"
    '<w:rFonts w:ascii="Calibri" w:eastAsia="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
    '<w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="en-US"/></w:rPr></w:rPrDefault>'
    '<w:pPrDefault><w:pPr><w:spacing w:after="160" w:line="259" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
    "</w:docDefaults>"
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>'
    '<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="240" w:line="240" w:lineRule="auto"/>'
    '<w:contextualSpacing/></w:pPr><w:rPr><w:rFonts w:ascii="Calibri Light" w:hAnsi="Calibri Light"/>'
    '<w:kern w:val="28"/><w:sz w:val="56"/><w:szCs w:val="56"/></w:rPr></w:style>'
    + "".join(
        f'<w:style w:type="paragraph" w:styleId="Heading{level}"><w:name w:val="heading {level}"/>'
        f'<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:uiPriority w:val="9"/><w:qFormat/>'
        f'<w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="{before}" w:after="{after}"/>'
        f'<w:outlineLvl w:val="{level - 1}"/></w:pPr><w:rPr><w:rFonts w:ascii="Calibri Light" w:hAnsi="Calibri Light"/>'
        f'<w:color w:val="{color}"/><w:sz w:val="{size}"/><w:szCs w:val="{size}"/></w:rPr></w:style>'
        for level, before, after, color, size in (
            (1, 360, 120, "2F5496", 32),
            (2, 240, 80, "2F5496", 26),
            (3, 200, 60, "1F3763", 24),
        )
    )
    + '<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/>'
    '<w:basedOn w:val="Normal"/><w:uiPriority w:val="34"/><w:qFormat/><w:pPr><w:ind w:left="720"/>'
    "<w:contextualSpacing/></w:pPr></w:style>"
    '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/>'
    '<w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar><w:top w:w="0" w:type="dxa"/>'
    '<w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/><w:right w:w="108" w:type="dxa"/>'
    "</w:tblCellMar></w:tblPr></w:style>"
    '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:basedOn w:val="TableNormal"/>'
    "<w:tblPr><w:tblBorders>"
    + "".join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    + "</w:tblBorders></w:tblPr></w:style></w:styles>"
)


def _numbering(numbered_lists: int) -> str:
    """Bullets are list 1; each numbered list is a list of its own (2, 3...), so each starts again at 1."""
    level = '<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl>'
    numbered = "".join(
        f'<w:num w:numId="{number}"><w:abstractNumId w:val="1"/>'
        '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride></w:num>'
        for number in range(2, numbered_lists + 2)
    )
    return (
        f"{HEAD}<w:numbering {W_NS}>"
        '<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/>'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/>'
        f"{level}</w:abstractNum>"
        '<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="hybridMultilevel"/>'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/>'
        f'{level}</w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>{numbered}</w:numbering>'
    )


def _w_run(text: str, *, bold: bool = False) -> str:
    """A run of text; a line break for each newline and a tab for each tab."""
    pieces = []
    for number, line in enumerate(text.split("\n")):
        if number:
            pieces.append("<w:br/>")
        for index, part in enumerate(line.split("\t")):
            if index:
                pieces.append("<w:tab/>")
            if part:
                pieces.append(f'<w:t xml:space="preserve">{escape(part)}</w:t>')
    if not pieces:
        return ""
    return f"<w:r>{'<w:rPr><w:b/></w:rPr>' if bold else ''}{''.join(pieces)}</w:r>"


def _w_paragraph(
    text: str, style: str | None = None, *, list_id: int | None = None, cell: bool = False, bold: bool = False
) -> str:
    props = []
    if style:
        props.append(f'<w:pStyle w:val="{style}"/>')
    if list_id is not None:
        props.append(f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{list_id}"/></w:numPr>')
    if cell:
        props.append('<w:spacing w:after="0" w:line="240" w:lineRule="auto"/>')
    shown = f"<w:pPr>{''.join(props)}</w:pPr>" if props else ""
    return f"<w:p>{shown}{_w_run(text, bold=bold)}</w:p>"


def _w_table(rows: list[list[str]], header: bool) -> str:
    columns = max(len(row) for row in rows)
    width = TEXT_WIDTH // columns
    grid = "".join(f'<w:gridCol w:w="{width}"/>' for _ in range(columns))
    shown = []
    for number, row in enumerate(rows):
        first = header and number == 0
        cells = "".join(
            f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>{_w_paragraph(text, cell=True, bold=first)}</w:tc>'
            for text in [*row, *[""] * (columns - len(row))]
        )
        shown.append(f"<w:tr>{'<w:trPr><w:tblHeader/></w:trPr>' if first else ''}{cells}</w:tr>")
    return (
        '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/>'
        '<w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="1" w:lastColumn="0" w:noHBand="0" '
        f'w:noVBand="1"/></w:tblPr><w:tblGrid>{grid}</w:tblGrid>{"".join(shown)}</w:tbl>'
    )


class WordBlock(ToolArgs):
    kind: Literal["heading", "paragraph", "bullets", "numbered", "table"] = Field(
        description=(
            "heading: a heading (text, level). paragraph: body text (text; each line a paragraph of its own). "
            "bullets / numbered: a bulleted or numbered list (items). table: a table (rows)."
        )
    )
    text: str | None = Field(default=None, max_length=20_000, description="For heading and paragraph.")
    level: int = Field(default=1, ge=1, le=3, description="For heading: 1 (main), 2 or 3.")
    items: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="For bullets and numbered: one entry per item, without bullet characters or numbers.",
    )
    rows: list[list[Value]] | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="For table: the rows, each a list of its cells' text from left to right (up to 20 columns).",
    )
    header_row: bool = Field(default=True, description="For table: the first row is a bold header. Default true.")

    @model_validator(mode="after")
    def _complete(self) -> Self:
        needs = {"heading": "text", "paragraph": "text", "bullets": "items", "numbered": "items", "table": "rows"}
        wanted = needs[self.kind]
        for field_name in ("text", "items", "rows"):
            given = getattr(self, field_name) is not None
            if field_name == wanted and not given:
                raise ValueError(f"{self.kind} needs {wanted}")
            if field_name != wanted and given:
                raise ValueError(f"{field_name} isn't for {self.kind}")
        if self.text is not None:
            self.text = clean_text(self.text)
        if self.items is not None:
            self.items = [clean_text(item) for item in self.items]
        if self.rows is not None:
            if any(len(row) > 20 for row in self.rows) or not any(self.rows):
                raise ValueError("a table has 1 to 20 columns")
        return self


class CreateWordArgs(NewFileArgs):
    title: str | None = Field(default=None, max_length=500, description="A title at the top, in the Title style.")
    blocks: list[WordBlock] = Field(
        default_factory=list,
        max_length=300,
        description="What the document holds, in order: headings, paragraphs, bulleted and numbered lists, tables.",
    )


def word_document(args: CreateWordArgs) -> tuple[bytes, str]:
    body: list[str] = []
    counts = {"heading": 0, "paragraph": 0, "list": 0, "table": 0}
    numbered = 0
    if args.title:
        body.append(_w_paragraph(clean_text(args.title).replace("\n", " "), "Title"))
    for number, block in enumerate(args.blocks):
        if block.kind == "heading":
            body.append(_w_paragraph((block.text or "").replace("\n", " "), f"Heading{block.level}"))
            counts["heading"] += 1
        elif block.kind == "paragraph":
            lines = (block.text or "").split("\n")
            body.extend(_w_paragraph(line) for line in lines)
            counts["paragraph"] += len(lines)
        elif block.kind in ("bullets", "numbered"):
            if block.kind == "numbered":
                numbered += 1
            list_id = 1 if block.kind == "bullets" else numbered + 1
            body.extend(_w_paragraph(item, "ListParagraph", list_id=list_id) for item in block.items or [])
            counts["list"] += 1
        else:
            rows = [[_text(value) for value in row] for row in block.rows or []]
            body.append(_w_table(rows, block.header_row))
            counts["table"] += 1
            # Word joins two tables that touch into one, and wants a paragraph between a table and the page's end.
            following = args.blocks[number + 1] if number + 1 < len(args.blocks) else None
            if following is None or following.kind == "table":
                body.append("<w:p/>")
    section = (
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
        'w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
    )
    document = (
        f'{HEAD}<w:document {W_NS} xmlns:r="{REL}"><w:body>{"".join(body) or "<w:p/>"}{section}</w:body></w:document>'
    )
    parts = {
        "[Content_Types].xml": _content_types(
            {
                "word/document.xml": f"{CT}.wordprocessingml.document.main+xml",
                "word/styles.xml": f"{CT}.wordprocessingml.styles+xml",
                "word/numbering.xml": f"{CT}.wordprocessingml.numbering+xml",
            }
        ),
        "_rels/.rels": _relationships(("rId1", "officeDocument", "word/document.xml")),
        "word/document.xml": document,
        "word/_rels/document.xml.rels": _relationships(
            ("rId1", "styles", "styles.xml"), ("rId2", "numbering", "numbering.xml")
        ),
        "word/styles.xml": _WORD_STYLES,
        "word/numbering.xml": _numbering(numbered),
    }
    described = ["a title"] if args.title else []
    described += [_plural(count, word) for word, count in counts.items() if count]
    return _package(parts), _listed(described) or "nothing in it yet"


def _listed(things: Sequence[str]) -> str:
    if len(things) <= 1:
        return "".join(things)
    return ", ".join(things[:-1]) + f" and {things[-1]}"


class CreateWord(CreateOfficeFile):
    name = "create_word_document"
    description = (
        "Create a new Word (.docx) document in the user's own folders (Documents unless they name another): a title, "
        "headings (levels 1-3), paragraphs, bulleted and numbered lists and tables (first row a bold header). Written "
        "as a file, without Word; never replaces an existing file: if the name is taken, the new one gets ' (2)' "
        "added. To change an existing document use word_outline and edit_word instead."
    )
    Args = CreateWordArgs
    extension = ".docx"
    kind = "Word document"
    fallback = "Document"
    tools = "word_outline and edit_word"

    def build(self, args: CreateWordArgs) -> tuple[bytes, str]:
        return word_document(args)


# --------------------------------------------------------------------------- PowerPoint

SLIDE_W, SLIDE_H = 12_192_000, 6_858_000  # 16:9, in EMU

_THEME = (
    f'{HEAD}<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office Theme">'
    '<a:themeElements><a:clrScheme name="Office">'
    '<a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1><a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
    + "".join(
        f'<a:{slot}><a:srgbClr val="{color}"/></a:{slot}>'
        for slot, color in (
            ("dk2", "44546A"),
            ("lt2", "E7E6E6"),
            ("accent1", "4472C4"),
            ("accent2", "ED7D31"),
            ("accent3", "A5A5A5"),
            ("accent4", "FFC000"),
            ("accent5", "5B9BD5"),
            ("accent6", "70AD47"),
            ("hlink", "0563C1"),
            ("folHlink", "954F72"),
        )
    )
    + '</a:clrScheme><a:fontScheme name="Office">'
    '<a:majorFont><a:latin typeface="Calibri Light"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>'
    '<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>'
    '<a:fmtScheme name="Office"><a:fillStyleLst>'
    + '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>' * 3
    + "</a:fillStyleLst><a:lnStyleLst>"
    + "".join(
        f'<a:ln w="{width}" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:prstDash val="solid"/><a:miter lim="800000"/></a:ln>'
        for width in (6350, 12700, 19050)
    )
    + "</a:lnStyleLst><a:effectStyleLst>"
    + "<a:effectStyle><a:effectLst/></a:effectStyle>" * 3
    + "</a:effectStyleLst><a:bgFillStyleLst>"
    + '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>' * 3
    + "</a:bgFillStyleLst></a:fmtScheme></a:themeElements><a:objectDefaults/><a:extraClrSchemeLst/></a:theme>"
)
_GROUP = (
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm>'
    '<a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
)
_CLR_MAP = (
    'bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" '
    'accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"'
)
_EMPTY_TEXT = '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="en-US"/></a:p></p:txBody>'


def _font(size: int, major: bool = False) -> str:
    kind = "mj" if major else "mn"
    return (
        f'<a:defRPr sz="{size}" kern="1200"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill>'
        f'<a:latin typeface="+{kind}-lt"/><a:ea typeface="+{kind}-ea"/><a:cs typeface="+{kind}-cs"/></a:defRPr>'
    )


def _shape(
    shape_id: int,
    name: str,
    placeholder: str,
    box: tuple[int, int, int, int] | None,
    text: str = _EMPTY_TEXT,
) -> str:
    """A placeholder shape; ``box`` (x, y, width, height) when it has a place of its own rather than its layout's."""
    if box is None:
        properties = "<p:spPr/>"
    else:
        x, y, cx, cy = box
        properties = (
            f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
        )
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="{name}"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
        f"<p:nvPr>{placeholder}</p:nvPr></p:nvSpPr>{properties}{text}</p:sp>"
    )


_TITLE_BOX = (838_200, 365_125, 10_515_600, 1_325_563)
_BODY_BOX = (838_200, 1_825_625, 10_515_600, 4_351_338)
_MASTER = (
    f'{HEAD}<p:sldMaster {PML_NS}><p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg>'
    f"<p:spTree>{_GROUP}"
    + _shape(
        2,
        "Title Placeholder 1",
        '<p:ph type="title"/>',
        _TITLE_BOX,
        '<p:txBody><a:bodyPr anchor="ctr"><a:normAutofit/></a:bodyPr><a:lstStyle/><a:p><a:endParaRPr lang="en-US"/>'
        "</a:p></p:txBody>",
    )
    + _shape(
        3,
        "Text Placeholder 2",
        '<p:ph type="body" idx="1"/>',
        _BODY_BOX,
        '<p:txBody><a:bodyPr><a:normAutofit/></a:bodyPr><a:lstStyle/><a:p><a:endParaRPr lang="en-US"/></a:p>'
        "</p:txBody>",
    )
    + f"</p:spTree></p:cSld><p:clrMap {_CLR_MAP}/>"
    '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/><p:sldLayoutId id="2147483650" r:id="rId2"/>'
    "</p:sldLayoutIdLst><p:txStyles>"
    '<p:titleStyle><a:lvl1pPr algn="l" defTabSz="914400" rtl="0" eaLnBrk="1" latinLnBrk="0" hangingPunct="1">'
    '<a:lnSpc><a:spcPct val="90000"/></a:lnSpc><a:spcBef><a:spcPct val="0"/></a:spcBef><a:buNone/>'
    f"{_font(4400, major=True)}</a:lvl1pPr></p:titleStyle><p:bodyStyle>"
    + "".join(
        f'<a:lvl{level}pPr marL="{margin}" indent="-228600" algn="l" defTabSz="914400" rtl="0" eaLnBrk="1" '
        'latinLnBrk="0" hangingPunct="1"><a:lnSpc><a:spcPct val="90000"/></a:lnSpc><a:spcBef><a:spcPts val="1000"/>'
        f'</a:spcBef><a:buFont typeface="Arial"/><a:buChar char="•"/>{_font(size)}</a:lvl{level}pPr>'
        for level, margin, size in ((1, 228_600, 2800), (2, 685_800, 2400), (3, 1_143_000, 2000))
    )
    + '</p:bodyStyle><p:otherStyle><a:defPPr><a:defRPr lang="en-US"/></a:defPPr></p:otherStyle></p:txStyles>'
    "</p:sldMaster>"
)
_TITLE_LAYOUT = (
    f'{HEAD}<p:sldLayout {PML_NS} type="title" preserve="1"><p:cSld name="Title Slide"><p:spTree>{_GROUP}'
    + _shape(
        2,
        "Title 1",
        '<p:ph type="ctrTitle"/>',
        (1_524_000, 1_122_363, 9_144_000, 2_387_600),
        '<p:txBody><a:bodyPr anchor="b"/><a:lstStyle><a:lvl1pPr algn="ctr"><a:defRPr sz="6000"/></a:lvl1pPr>'
        '</a:lstStyle><a:p><a:endParaRPr lang="en-US"/></a:p></p:txBody>',
    )
    + _shape(
        3,
        "Subtitle 2",
        '<p:ph type="subTitle" idx="1"/>',
        (1_524_000, 3_602_038, 9_144_000, 1_655_762),
        '<p:txBody><a:bodyPr/><a:lstStyle><a:lvl1pPr marL="0" indent="0" algn="ctr"><a:buNone/>'
        '<a:defRPr sz="2400"/></a:lvl1pPr></a:lstStyle><a:p><a:endParaRPr lang="en-US"/></a:p></p:txBody>',
    )
    + "</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"
)
_CONTENT_LAYOUT = (
    f'{HEAD}<p:sldLayout {PML_NS} type="obj" preserve="1"><p:cSld name="Title and Content"><p:spTree>{_GROUP}'
    + _shape(2, "Title 1", '<p:ph type="title"/>', _TITLE_BOX)
    + _shape(3, "Content Placeholder 2", '<p:ph idx="1"/>', _BODY_BOX)
    + "</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"
)
_NOTES_MASTER = (
    f'{HEAD}<p:notesMaster {PML_NS}><p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg>'
    f"<p:spTree>{_GROUP}"
    '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Slide Image Placeholder 1"/><p:cNvSpPr><a:spLocks noGrp="1" noRot="1" '
    'noChangeAspect="1"/></p:cNvSpPr><p:nvPr><p:ph type="sldImg" idx="2"/></p:nvPr></p:nvSpPr><p:spPr><a:xfrm>'
    '<a:off x="685800" y="1143000"/><a:ext cx="5486400" cy="3086100"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/>'
    '</a:prstGeom><a:noFill/><a:ln w="12700"><a:solidFill><a:prstClr val="black"/></a:solidFill></a:ln></p:spPr>'
    "</p:sp>"
    + _shape(
        3, "Notes Placeholder 2", '<p:ph type="body" sz="quarter" idx="3"/>', (685_800, 4_400_550, 5_486_400, 3_600_450)
    )
    + f"</p:spTree></p:cSld><p:clrMap {_CLR_MAP}/><p:notesStyle>"
    f'<a:lvl1pPr marL="0" algn="l" defTabSz="914400" rtl="0" eaLnBrk="1" latinLnBrk="0" hangingPunct="1">{_font(1200)}'
    "</a:lvl1pPr></p:notesStyle></p:notesMaster>"
)


def _a_paragraph(text: str) -> str:
    """A paragraph of slide text; a line break for each newline."""
    pieces = []
    for number, line in enumerate(text.split("\n")):
        if number:
            pieces.append('<a:br><a:rPr lang="en-US"/></a:br>')
        if line:
            pieces.append(f'<a:r><a:rPr lang="en-US" dirty="0"/><a:t>{escape(line)}</a:t></a:r>')
    return f'<a:p>{"".join(pieces)}<a:endParaRPr lang="en-US" dirty="0"/></a:p>'


def _text_body(paragraphs: Sequence[str]) -> str:
    return f"<p:txBody><a:bodyPr/><a:lstStyle/>{''.join(_a_paragraph(p) for p in paragraphs) or _a_paragraph('')}</p:txBody>"


class SlideSpec(ToolArgs):
    layout: Literal["title", "content"] = Field(
        default="content",
        description="title: a title slide (title and subtitle, centred). content: a title and bullet points. "
        "Default content.",
    )
    title: str = Field(default="", max_length=500, description="The slide's title.")
    subtitle: str | None = Field(default=None, max_length=1000, description="For a title slide: the line under it.")
    bullets: list[str] | None = Field(
        default=None,
        max_length=20,
        description="For a content slide: its bullet points, one per entry, without bullet characters.",
    )
    notes: str | None = Field(
        default=None, max_length=10_000, description="Speaker notes for the slide; each line a paragraph."
    )

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if self.layout == "title" and self.bullets is not None:
            raise ValueError("bullets are for a content slide")
        if self.layout == "content" and self.subtitle is not None:
            raise ValueError("subtitle is for a title slide")
        self.title = clean_text(self.title)
        if self.subtitle is not None:
            self.subtitle = clean_text(self.subtitle)
        if self.bullets is not None:
            self.bullets = [clean_text(each) for each in self.bullets]
        if self.notes is not None:
            self.notes = clean_text(self.notes)
        return self


class CreatePowerPointArgs(NewFileArgs):
    slides: list[SlideSpec] = Field(
        min_length=1, max_length=60, description="The slides in order; usually a title slide first."
    )


def _slide(spec: SlideSpec) -> str:
    title = _text_body([spec.title])
    if spec.layout == "title":
        shapes = _shape(2, "Title 1", '<p:ph type="ctrTitle"/>', None, title)
        if spec.subtitle:
            shapes += _shape(3, "Subtitle 2", '<p:ph type="subTitle" idx="1"/>', None, _text_body([spec.subtitle]))
    else:
        shapes = _shape(2, "Title 1", '<p:ph type="title"/>', None, title)
        if spec.bullets:
            body = _text_body(spec.bullets).replace("<a:bodyPr/>", "<a:bodyPr><a:normAutofit/></a:bodyPr>", 1)
            shapes += _shape(3, "Content Placeholder 2", '<p:ph idx="1"/>', None, body)
    return (
        f"{HEAD}<p:sld {PML_NS}><p:cSld><p:spTree>{_GROUP}{shapes}</p:spTree></p:cSld>"
        "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
    )


def _notes(text: str) -> str:
    image = (
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Slide Image Placeholder 1"/><p:cNvSpPr><a:spLocks noGrp="1" '
        'noRot="1" noChangeAspect="1"/></p:cNvSpPr><p:nvPr><p:ph type="sldImg"/></p:nvPr></p:nvSpPr><p:spPr/></p:sp>'
    )
    body = _shape(3, "Notes Placeholder 2", '<p:ph type="body" idx="1"/>', None, _text_body(text.split("\n")))
    return (
        f"{HEAD}<p:notes {PML_NS}><p:cSld><p:spTree>{_GROUP}{image}{body}</p:spTree></p:cSld>"
        "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:notes>"
    )


def presentation(args: CreatePowerPointArgs) -> tuple[bytes, str]:
    slides = args.slides
    overrides = {
        "ppt/presentation.xml": f"{CT}.presentationml.presentation.main+xml",
        "ppt/slideMasters/slideMaster1.xml": f"{CT}.presentationml.slideMaster+xml",
        "ppt/slideLayouts/slideLayout1.xml": f"{CT}.presentationml.slideLayout+xml",
        "ppt/slideLayouts/slideLayout2.xml": f"{CT}.presentationml.slideLayout+xml",
        "ppt/notesMasters/notesMaster1.xml": f"{CT}.presentationml.notesMaster+xml",
        "ppt/theme/theme1.xml": f"{CT}.theme+xml",
        "ppt/theme/theme2.xml": f"{CT}.theme+xml",
        "ppt/presProps.xml": f"{CT}.presentationml.presProps+xml",
        "ppt/viewProps.xml": f"{CT}.presentationml.viewProps+xml",
        "ppt/tableStyles.xml": f"{CT}.presentationml.tableStyles+xml",
    }
    master_rel = ("rId1", "slideMaster", "../slideMasters/slideMaster1.xml")
    parts = {
        "_rels/.rels": _relationships(("rId1", "officeDocument", "ppt/presentation.xml")),
        "ppt/slideMasters/slideMaster1.xml": _MASTER,
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": _relationships(
            ("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml"),
            ("rId2", "slideLayout", "../slideLayouts/slideLayout2.xml"),
            ("rId3", "theme", "../theme/theme1.xml"),
        ),
        "ppt/slideLayouts/slideLayout1.xml": _TITLE_LAYOUT,
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": _relationships(master_rel),
        "ppt/slideLayouts/slideLayout2.xml": _CONTENT_LAYOUT,
        "ppt/slideLayouts/_rels/slideLayout2.xml.rels": _relationships(master_rel),
        "ppt/notesMasters/notesMaster1.xml": _NOTES_MASTER,
        "ppt/notesMasters/_rels/notesMaster1.xml.rels": _relationships(("rId1", "theme", "../theme/theme2.xml")),
        "ppt/theme/theme1.xml": _THEME,
        "ppt/theme/theme2.xml": _THEME,
        "ppt/presProps.xml": f"{HEAD}<p:presentationPr {PML_NS}/>",
        "ppt/viewProps.xml": f"{HEAD}<p:viewPr {PML_NS}/>",
        "ppt/tableStyles.xml": (
            f'{HEAD}<a:tblStyleLst xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'def="{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}"/>'
        ),
    }
    slide_rels = []
    noted = 0
    for number, spec in enumerate(slides, 1):
        slide_part = f"ppt/slides/slide{number}.xml"
        overrides[slide_part] = f"{CT}.presentationml.slide+xml"
        parts[slide_part] = _slide(spec)
        layout = 1 if spec.layout == "title" else 2
        rels = [("rId1", "slideLayout", f"../slideLayouts/slideLayout{layout}.xml")]
        if spec.notes:
            noted += 1
            notes_part = f"ppt/notesSlides/notesSlide{number}.xml"
            overrides[notes_part] = f"{CT}.presentationml.notesSlide+xml"
            parts[notes_part] = _notes(spec.notes)
            parts[f"ppt/notesSlides/_rels/notesSlide{number}.xml.rels"] = _relationships(
                ("rId1", "notesMaster", "../notesMasters/notesMaster1.xml"),
                ("rId2", "slide", f"../slides/slide{number}.xml"),
            )
            rels.append(("rId2", "notesSlide", f"../notesSlides/notesSlide{number}.xml"))
        parts[f"ppt/slides/_rels/slide{number}.xml.rels"] = _relationships(*rels)
        slide_rels.append((f"rId{number + 10}", "slide", f"slides/slide{number}.xml"))
    slide_ids = "".join(f'<p:sldId id="{255 + n}" r:id="rId{n + 10}"/>' for n in range(1, len(slides) + 1))
    parts["ppt/presentation.xml"] = (
        f'{HEAD}<p:presentation {PML_NS} saveSubsetFonts="1">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        '<p:notesMasterIdLst><p:notesMasterId r:id="rId2"/></p:notesMasterIdLst>'
        f'<p:sldIdLst>{slide_ids}</p:sldIdLst><p:sldSz cx="{SLIDE_W}" cy="{SLIDE_H}"/>'
        '<p:notesSz cx="6858000" cy="9144000"/></p:presentation>'
    )
    parts["ppt/_rels/presentation.xml.rels"] = _relationships(
        ("rId1", "slideMaster", "slideMasters/slideMaster1.xml"),
        ("rId2", "notesMaster", "notesMasters/notesMaster1.xml"),
        ("rId3", "theme", "theme/theme1.xml"),
        ("rId4", "presProps", "presProps.xml"),
        ("rId5", "viewProps", "viewProps.xml"),
        ("rId6", "tableStyles", "tableStyles.xml"),
        *slide_rels,
    )
    ordered = {"[Content_Types].xml": _content_types(overrides), **parts}
    contents = _plural(len(slides), "slide")
    if noted:
        contents += f" ({noted} with speaker notes)"
    return _package(ordered), contents


class CreatePowerPoint(CreateOfficeFile):
    name = "create_powerpoint"
    description = (
        "Create a new PowerPoint (.pptx) presentation in the user's own folders (Documents unless they name "
        "another): title slides (title and subtitle) and content slides (a title and bullet points), each with "
        "optional speaker notes; widescreen, in the plain Office theme. Written as a file, without PowerPoint; never "
        "replaces an existing file: if the name is taken, the new one gets ' (2)' added. To change an existing "
        "presentation use powerpoint_outline and edit_powerpoint instead."
    )
    Args = CreatePowerPointArgs
    extension = ".pptx"
    kind = "presentation"
    fallback = "Presentation"
    tools = "powerpoint_outline and edit_powerpoint"

    def build(self, args: CreatePowerPointArgs) -> tuple[bytes, str]:
        return presentation(args)


# --------------------------------------------------------------------------- Excel

_EXCEL_STYLES = (
    f"{HEAD}<styleSheet {S_NS}>"
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/><family val="2"/><scheme val="minor"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/><scheme val="minor"/></font></fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFD9E1F2"/><bgColor indexed="64"/></patternFill></fill>'
    "</fills>"
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)
HEADER_STYLE = "1"
MAX_WIDTH = 60  # characters a column is made wide at most


class SheetSpec(ToolArgs):
    name: str = Field(min_length=1, max_length=31, description="The sheet's tab name (up to 31 characters).")
    header: list[Value] | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Column headings for row 1: bold, shaded, and kept in view while scrolling. Omit for none.",
    )
    rows: list[list[Value]] = Field(
        default_factory=list,
        max_length=2000,
        description=(
            "The rows below the header (from row 2 when there's a header, else row 1), each its cells from column A "
            "on, as typed into Excel: numbers, text, TRUE/FALSE, or formulas starting with '=' (e.g. '=SUM(B2:B9)'). "
            "'' or null leaves a cell empty; a leading apostrophe keeps a value as text (\"'007\")."
        ),
    )

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if _SHEET_NAME_BAD.search(self.name) or self.name.startswith("'") or self.name.endswith("'"):
            raise ValueError("a sheet name can't hold [ ] : * ? / \\ or start or end with an apostrophe")
        if any(len(row) > 200 for row in self.rows):
            raise ValueError("a row has at most 200 cells")
        return self


class CreateExcelArgs(NewFileArgs):
    sheets: list[SheetSpec] = Field(
        min_length=1, max_length=20, description="The workbook's sheets, in order; the first is shown when it opens."
    )

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        names = [sheet.name.casefold() for sheet in self.sheets]
        if len(set(names)) != len(names):
            raise ValueError("each sheet needs a name of its own")
        return self


def _cell(row: int, column: int, value: str, style: str | None = None) -> tuple[str, int]:
    """A cell's XML, as Excel takes what's typed into it, and how many characters wide it shows."""
    reference = cell_name(row, column)
    styled = f' s="{style}"' if style else ""
    as_text = value.startswith("'") and len(value) > 1
    kind, text = typed(value[1:] if as_text else value, as_text or style == HEADER_STYLE)
    if kind == "clear":
        return "", 0
    if kind == "formula":
        if len(text) > MAX_FORMULA:
            raise Refused(f"The formula for {reference} is longer than Excel allows.")
        return f'<c r="{reference}"{styled}><f>{escape(text)}</f></c>', 10
    if kind == "number":
        return f'<c r="{reference}"{styled}><v>{text}</v></c>', len(text)
    if kind == "bool":
        return f'<c r="{reference}"{styled} t="b"><v>{text}</v></c>', 5
    text = text[:MAX_TEXT]
    widest = max((len(line) for line in text.split("\n")), default=0)
    return f'<c r="{reference}"{styled} t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>', widest


def _sheet(spec: SheetSpec, first: bool) -> tuple[str, int]:
    """A worksheet part, and how many rows it fills."""
    widths: dict[int, int] = {}
    rows_xml = []
    lines: list[tuple[list[Value], str | None]] = []
    if spec.header is not None:
        lines.append((spec.header, HEADER_STYLE))
    lines.extend((row, None) for row in spec.rows)
    filled = 0
    for number, (values, style) in enumerate(lines, 1):
        cells = []
        for column, value in enumerate(values, 1):
            shown, width = _cell(number, column, _text(value), style)
            if shown:
                cells.append(shown)
                widths[column] = max(widths.get(column, 0), width + (2 if style else 0))
        if cells:
            rows_xml.append(f'<row r="{number}">{"".join(cells)}</row>')
            filled = number
    pane = (
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>'
        if spec.header is not None
        else ""
    )
    selected = ' tabSelected="1"' if first else ""
    columns = "".join(
        f'<col min="{column}" max="{column}" width="{min(max(width + 2, 9), MAX_WIDTH)}" customWidth="1"/>'
        for column, width in sorted(widths.items())
        if width + 2 > 9
    )
    dimension = f"A1:{column_letters(max(widths))}{filled}" if widths and filled else "A1"
    return (
        f'{HEAD}<worksheet {S_NS}><dimension ref="{dimension}"/>'
        f'<sheetViews><sheetView workbookViewId="0"{selected}>{pane}</sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        f"{f'<cols>{columns}</cols>' if columns else ''}<sheetData>{''.join(rows_xml)}</sheetData>"
        '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/></worksheet>'
    ), filled


def workbook(args: CreateExcelArgs) -> tuple[bytes, str]:
    overrides = {
        "xl/workbook.xml": f"{CT}.spreadsheetml.sheet.main+xml",
        "xl/styles.xml": f"{CT}.spreadsheetml.styles+xml",
    }
    parts = {"_rels/.rels": _relationships(("rId1", "officeDocument", "xl/workbook.xml"))}
    listed, rels, described = [], [], []
    for number, spec in enumerate(args.sheets, 1):
        part = f"xl/worksheets/sheet{number}.xml"
        overrides[part] = f"{CT}.spreadsheetml.worksheet+xml"
        parts[part], filled = _sheet(spec, number == 1)
        listed.append(f'<sheet name="{_attr(spec.name)}" sheetId="{number}" r:id="rId{number}"/>')
        rels.append((f"rId{number}", "worksheet", f"worksheets/sheet{number}.xml"))
        described.append(f"{spec.name} ({_plural(filled, 'row')})")
    rels.append((f"rId{len(args.sheets) + 1}", "styles", "styles.xml"))
    parts["xl/workbook.xml"] = (
        f'{HEAD}<workbook {S_NS}><bookViews><workbookView activeTab="0"/></bookViews>'
        f'<sheets>{"".join(listed)}</sheets><calcPr calcId="191029" fullCalcOnLoad="1"/></workbook>'
    )
    parts["xl/_rels/workbook.xml.rels"] = _relationships(*rels)
    parts["xl/styles.xml"] = _EXCEL_STYLES
    ordered = {"[Content_Types].xml": _content_types(overrides), **parts}
    return _package(ordered), f"{_plural(len(args.sheets), 'sheet')}: {', '.join(described)}"


class CreateExcel(CreateOfficeFile):
    name = "create_excel_workbook"
    description = (
        "Create a new Excel (.xlsx) workbook in the user's own folders (Documents unless they name another): one or "
        "more sheets, each with an optional header row (bold, kept in view) and rows of numbers, text, TRUE/FALSE "
        "and formulas such as '=SUM(B2:B9)', which Excel works out when it opens the file. Written as a file, "
        "without Excel; never replaces an existing file: if the name is taken, the new one gets ' (2)' added. To "
        "change an existing workbook use excel_outline and edit_excel instead."
    )
    Args = CreateExcelArgs
    extension = ".xlsx"
    kind = "Excel workbook"
    fallback = "Workbook"
    tools = "excel_outline and edit_excel"

    def build(self, args: CreateExcelArgs) -> tuple[bytes, str]:
        return workbook(args)


TOOLS = [CreateWord, CreatePowerPoint, CreateExcel]
