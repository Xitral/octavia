"""The user's Word files, edited as files rather than by clicking in Word: ``word_outline`` lists a document's
paragraphs and tables, each with an id, and ``edit_word`` replaces a paragraph's text, inserts and deletes paragraphs
and sets the text of table cells.

Only the main text is read and edited (not headers, footers, footnotes, comments or text boxes), so only the
document part is rewritten (see ``_office`` for how the file is saved, and why a file open in Word is left alone).
Ids number the paragraphs and tables of the body in order, as the outline showed them: all of one call's edits use
those ids, however the earlier edits moved things.

Replaced text keeps its formatting as the PowerPoint tools' does: what the new text has in common with the old at
its start and end keeps the formatting it had (a bold "Note:" stays bold), and what's new takes the formatting of the
text it replaces. Each run of text keeps its place, so text in a link stays in the link, and line and page breaks,
pictures and fields stay where they are. A new paragraph is formatted like the one it's added next to (its style,
list and indent, and the font most of its text has), unless a style is named for it.
"""

from __future__ import annotations

import asyncio
import copy
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Literal, Self
from xml.etree import ElementTree
from xml.etree.ElementTree import Element

from pydantic import Field, model_validator

from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions._office import (
    MAX_EDITS,
    MAX_OUTLINE,
    EditError,
    FileArgs,
    busy_message,
    busy_reason,
    clean_text,
    resolve,
    save_parts,
    serialize,
)
from octavia.tools.extensions.files import FILES, FileMemory, KnownFolders, Refused, known_folder
from octavia.tools.extensions.powerpoint import _parse, _read, _related, _rels

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

_TEXT_TAGS = {f"{W}t", f"{W}tab", f"{W}noBreakHyphen"}
_BREAK_TAGS = {f"{W}br", f"{W}cr"}
# Inside a paragraph, what holds runs that are part of its text (deleted text, in w:del, isn't).
_INLINE = {f"{W}{tag}" for tag in ("hyperlink", "ins", "smartTag", "fldSimple", "customXml", "moveTo", "dir", "bdo")}
_TRACKING = {f"{W}{tag}" for tag in ("ins", "del", "moveFrom", "moveTo", "rPrChange", "pPrChange")}


def _app_busy(path: str) -> str | None:
    return busy_reason(path, shortened=True)


# --------------------------------------------------------------------------- reading


def _runs(parent: Element) -> Iterator[Element]:
    for child in parent:
        if child.tag == f"{W}r":
            yield child
        elif child.tag in _INLINE:
            yield from _runs(child)
        elif child.tag == f"{W}sdt" and (content := child.find(f"{W}sdtContent")) is not None:
            yield from _runs(content)


def _piece_text(piece: Element) -> str:
    if piece.tag == f"{W}tab":
        return "\t"
    if piece.tag == f"{W}noBreakHyphen":
        return "-"
    return piece.text or ""


def _run_text(run: Element) -> str:
    return "".join(_piece_text(piece) for piece in run if piece.tag in _TEXT_TAGS)


def paragraph_text(paragraph: Element, *, breaks: bool = False) -> str:
    """The paragraph's text; ``breaks``: with a space for each line break, as it's shown."""
    parts = []
    for run in _runs(paragraph):
        for piece in run:
            if piece.tag in _TEXT_TAGS:
                parts.append(_piece_text(piece))
            elif breaks and piece.tag in _BREAK_TAGS:
                parts.append(" ")
    return "".join(parts)


def _blocks(parent: Element) -> Iterator[tuple[Element, Element]]:
    """The paragraphs and tables of the body, in order, with the element each is in."""
    for child in parent:
        if child.tag in (f"{W}p", f"{W}tbl"):
            yield child, parent
        elif child.tag == f"{W}sdt" and (content := child.find(f"{W}sdtContent")) is not None:
            yield from _blocks(content)
        elif child.tag == f"{W}customXml":
            yield from _blocks(child)


def _ends_section(paragraph: Element) -> bool:
    return paragraph.find(f"{W}pPr/{W}sectPr") is not None


def _merged_below(cell: Element) -> bool:
    """The cell continues the one above it (vertically merged), so it has no text of its own."""
    merge = cell.find(f"{W}tcPr/{W}vMerge")
    return merge is not None and merge.get(f"{W}val", "continue") != "restart"


def cell_text(cell: Element) -> str:
    if _merged_below(cell):
        return "(merged with above)"
    return " / ".join(paragraph_text(p, breaks=True) for p in cell.findall(f"{W}p"))


@dataclass
class Styles:
    """The document's paragraph styles: id -> name, and which is the default."""

    names: dict[str, str] = field(default_factory=dict)
    default: str | None = None

    @classmethod
    def of(cls, root: Element | None) -> Styles:
        styles = cls()
        if root is None:
            return styles
        for style in root.iterfind(f"{W}style"):
            if style.get(f"{W}type") != "paragraph" or not (style_id := style.get(f"{W}styleId")):
                continue
            name_element = style.find(f"{W}name")
            name = name_element.get(f"{W}val", "") if name_element is not None else ""
            styles.names[style_id] = (name[:1].upper() + name[1:]) if name else style_id
            if style.get(f"{W}default") in ("1", "true") and styles.default is None:
                styles.default = style_id
        return styles

    def of_paragraph(self, paragraph: Element) -> str | None:
        style = paragraph.find(f"{W}pPr/{W}pStyle")
        return style.get(f"{W}val") if style is not None else self.default

    def name(self, style_id: str | None) -> str:
        return self.names.get(style_id or "", style_id or "Normal")

    def find(self, wanted: str) -> str:
        """The id of the style called ``wanted``. Raises EditError."""
        key = wanted.strip().casefold()
        for style_id, name in self.names.items():
            if key in (style_id.casefold(), name.casefold()):
                return style_id
        known = ", ".join(sorted(self.names.values())[:30]) or "none"
        raise EditError(f'The document has no paragraph style "{wanted}". Its styles: {known}.')


@dataclass
class Block:
    id: int
    element: Element
    parent: Element

    @property
    def is_table(self) -> bool:
        return self.element.tag == f"{W}tbl"


class Document:
    """A Word document, read: its body's paragraphs and tables, and its paragraph styles. Blocking."""

    def __init__(self, path: str) -> None:
        self.path = path
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            self.part = _related(_rels(archive, names, ""), "officeDocument") or "word/document.xml"
            self.root, self.namespaces = _parse(_read(archive, self.part))
            styles_part = _related(_rels(archive, names, self.part), "styles")
            self.styles = Styles.of(_parse(_read(archive, styles_part))[0] if styles_part in names else None)
        body = self.root.find(f"{W}body")
        if body is None:
            raise ValueError("it has no body text")
        self.blocks = [Block(number, element, parent) for number, (element, parent) in enumerate(_blocks(body), 1)]

    def block(self, block_id: int) -> Block:
        if not 1 <= block_id <= len(self.blocks):
            raise EditError(f"There's no #{block_id}; the document's ids go from 1 to {len(self.blocks)}.")
        return self.blocks[block_id - 1]

    def save(self) -> None:
        save_parts(self.path, {self.part: serialize(self.root, self.namespaces)})


# --------------------------------------------------------------------------- editing


def _text_pieces(text: str) -> list[Element]:
    pieces = []
    for number, part in enumerate(text.split("\t")):
        if number:
            pieces.append(Element(f"{W}tab"))
        if part:
            piece = Element(f"{W}t")
            piece.text = part
            if part != part.strip():
                piece.set(XML_SPACE, "preserve")
            pieces.append(piece)
    return pieces


def _formatting(source: Element | None) -> Element | None:
    """A copy of run formatting (or a paragraph mark's), without its tracked-change marks."""
    if source is None:
        return None
    formatting = copy.deepcopy(source)
    formatting.tag = f"{W}rPr"
    for child in [c for c in formatting if c.tag in _TRACKING]:
        formatting.remove(child)
    return formatting


def set_text(paragraph: Element, text: str) -> None:
    """Gives a paragraph new text, each character formatted like the one it replaces (see the module's docstring)."""
    runs = [run for run in _runs(paragraph) if any(piece.tag in _TEXT_TAGS for piece in run)]
    texts = [_run_text(run) for run in runs]
    old = "".join(texts)
    if text == old:
        return
    if not runs:
        if text:
            run = Element(f"{W}r")
            if (formatting := _formatting(paragraph.find(f"{W}pPr/{W}rPr"))) is not None:
                run.append(formatting)
            run.extend(_text_pieces(text))
            paragraph.append(run)
        return
    owner = [i for i, run_text in enumerate(texts) for _ in run_text]
    limit = min(len(old), len(text))
    start = 0
    while start < limit and old[start] == text[start]:
        start += 1
    end = 0
    while end < limit - start and old[-1 - end] == text[-1 - end]:
        end += 1
    # Each run keeps its place, so the runs own the new text in their order: what's new goes to the run where the
    # change starts, or, when everything before it changed, to the run with most of the text before the kept end.
    if start == 0:
        last = owner[len(old) - end] if end else len(runs) - 1
        middle = max(range(last + 1), key=lambda i: len(texts[i]))
    else:
        middle = owner[start] if start < len(old) else owner[-1]
    owners = [*owner[:start], *[middle] * (len(text) - start - end), *owner[len(old) - end :]]
    parents = {child: parent for parent in paragraph.iter() for child in parent}
    for i, run in enumerate(runs):
        chunk = "".join(char for char, who in zip(text, owners, strict=True) if who == i)
        pieces = [child for child in run if child.tag in _TEXT_TAGS]
        at = list(run).index(pieces[0])
        for piece in pieces:
            run.remove(piece)
        if chunk:
            for offset, piece in enumerate(_text_pieces(chunk)):
                run.insert(at + offset, piece)
        elif all(child.tag in (f"{W}rPr", f"{W}lastRenderedPageBreak") for child in run):
            parents[run].remove(run)


def set_style(paragraph: Element, style_id: str) -> None:
    properties = paragraph.find(f"{W}pPr")
    if properties is None:
        properties = Element(f"{W}pPr")
        paragraph.insert(0, properties)
    style = properties.find(f"{W}pStyle")
    if style is None:
        style = Element(f"{W}pStyle")
        properties.insert(0, style)
    style.set(f"{W}val", style_id)


def new_paragraph(reference: Element, text: str, style_id: str | None) -> Element:
    """A paragraph formatted like ``reference`` (a paragraph or table it's added next to), or in ``style_id``."""
    paragraph = Element(f"{W}p")
    formatting = None
    if style_id is not None:
        set_style(paragraph, style_id)
    else:
        properties = reference.find(f"{W}pPr") if reference.tag == f"{W}p" else None
        if properties is not None:
            kept = copy.deepcopy(properties)
            for child in [c for c in kept if c.tag in (f"{W}sectPr", f"{W}pPrChange")]:
                kept.remove(child)
            mark = kept.find(f"{W}rPr")
            if mark is not None:
                for child in [c for c in mark if c.tag in _TRACKING]:
                    mark.remove(child)
            paragraph.append(kept)
            formatting = _formatting(mark)
        written = [run for run in _runs(reference) if _run_text(run)] if reference.tag == f"{W}p" else []
        if written:
            formatting = _formatting(max(written, key=lambda run: len(_run_text(run))).find(f"{W}rPr"))
    if text:
        run = ElementTree.SubElement(paragraph, f"{W}r")
        if formatting is not None:
            run.append(formatting)
        run.extend(_text_pieces(text))
    return paragraph


Action = Literal["replace_text", "insert_after", "insert_before", "delete", "set_cell"]


class WordEdit(ToolArgs):
    id: int = Field(ge=1, description="Paragraph or table #id from word_outline.")
    action: Action = Field(description="set_cell sets a cell (row, column) of table #id.")
    text: str | None = Field(
        default=None,
        max_length=20_000,
        description="All the new text, a line per paragraph, no bullet characters or numbers.",
    )
    style: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Paragraph style, e.g. 'Heading 2'. Default: kept, or like #id.",
    )
    row: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if self.action == "delete":
            if self.text is not None or self.style is not None:
                raise ValueError("delete takes no text or style")
        elif self.text is None:
            raise ValueError(f"{self.action} needs text")
        else:
            self.text = clean_text(self.text)
        if self.action == "set_cell":
            if self.row is None or self.column is None:
                raise ValueError("set_cell needs row and column")
            if self.style is not None:
                raise ValueError("style isn't for set_cell")
        elif self.row is not None or self.column is not None:
            raise ValueError("row and column are only for set_cell")
        return self


def _snippet(text: str, limit: int = 50) -> str:
    text = text.replace("\n", " / ")
    return text if len(text) <= limit else text[: limit - 3] + "..."


class Editor:
    """Makes one call's edits on a document in memory. Raises EditError."""

    def __init__(self, document: Document) -> None:
        self.document = document
        self.deleted: set[int] = set()
        self.last_after: dict[int, Element] = {}  # block id -> the last paragraph added after it

    def _block(self, block_id: int) -> Block:
        block = self.document.block(block_id)
        if block_id in self.deleted:
            raise EditError(f"#{block_id} was deleted by an earlier edit.")
        return block

    def _insert(self, block: Block, lines: list[str], *, after: bool, style_id: str | None) -> None:
        for line in lines:
            paragraph = new_paragraph(block.element, line, style_id)
            if after:
                previous = self.last_after.get(block.id, block.element)
                block.parent.insert(list(block.parent).index(previous) + 1, paragraph)
                self.last_after[block.id] = paragraph
            else:
                block.parent.insert(list(block.parent).index(block.element), paragraph)

    def apply(self, edit: WordEdit) -> str:
        block = self._block(edit.id)
        style_id = self.document.styles.find(edit.style) if edit.style is not None else None
        if edit.action == "delete":
            if not block.is_table and _ends_section(block.element):
                raise EditError(f"#{block.id} ends a section of the document (its page layout), so I won't delete it.")
            block.parent.remove(block.element)
            self.deleted.add(block.id)
            return f"deleted {'table' if block.is_table else 'paragraph'} #{block.id}"
        lines = (edit.text or "").split("\n")
        if edit.action in ("insert_after", "insert_before"):
            self._insert(block, lines, after=edit.action == "insert_after", style_id=style_id)
            count = f"{len(lines)} paragraphs" if len(lines) > 1 else "a paragraph"
            return f"added {count} {'after' if edit.action == 'insert_after' else 'before'} #{block.id}"
        if edit.action == "replace_text":
            if block.is_table:
                raise EditError(f"#{block.id} is a table: use set_cell for its cells.")
            set_text(block.element, lines[0])
            if style_id is not None:
                set_style(block.element, style_id)
            self._insert(block, lines[1:], after=True, style_id=None)
            extra = f" (and added {len(lines) - 1} after it)" if len(lines) > 1 else ""
            return f"replaced the text of #{block.id}{extra}"
        return self._set_cell(block, edit, lines)

    def _set_cell(self, block: Block, edit: WordEdit, lines: list[str]) -> str:
        assert edit.row is not None and edit.column is not None
        if not block.is_table:
            raise EditError(f"#{block.id} is a paragraph, not a table.")
        rows = block.element.findall(f"{W}tr")
        if edit.row > len(rows):
            raise EditError(f"Table #{block.id} has {len(rows)} rows.")
        cells = rows[edit.row - 1].findall(f"{W}tc")
        if edit.column > len(cells):
            raise EditError(f"Row {edit.row} of table #{block.id} has {len(cells)} cells.")
        cell = cells[edit.column - 1]
        where = f"row {edit.row}, column {edit.column} of table #{block.id}"
        if _merged_below(cell):
            raise EditError(f"The cell at {where} is merged with the one above it: set that one instead.")
        if cell.find(f"{W}tbl") is not None:
            raise EditError(f"The cell at {where} holds a table of its own, which I can't edit.")
        paragraphs = cell.findall(f"{W}p")
        if not paragraphs:
            paragraphs = [ElementTree.SubElement(cell, f"{W}p")]
        for extra in paragraphs[len(lines) :]:
            cell.remove(extra)
        last = paragraphs[min(len(lines), len(paragraphs)) - 1]
        for number, line in enumerate(lines):
            if number < len(paragraphs):
                set_text(paragraphs[number], line)
                continue
            added = new_paragraph(last, line, None)
            cell.insert(list(cell).index(last) + 1, added)
            last = added
        return f"set the text at {where}"


# --------------------------------------------------------------------------- word_outline


class OutlineArgs(FileArgs):
    start: int | None = Field(default=None, ge=1, description="Show from this #id on.")


def outline(document: Document, name: str, start: int | None) -> str:
    styles = document.styles
    paragraphs = sum(1 for block in document.blocks if not block.is_table)
    tables = len(document.blocks) - paragraphs
    lines = [
        f"{name}: {paragraphs} paragraph{'' if paragraphs == 1 else 's'}, {tables} table{'' if tables == 1 else 's'}. "
        "Each: #id, [style] when not the default one, then its text; tables a line per row, cells separated by '|'."
    ]
    used = len(lines[0])
    for block in document.blocks:
        if start is not None and block.id < start:
            continue
        if block.is_table:
            rows = block.element.findall(f"{W}tr")
            shown = [f"#{block.id} table, {len(rows)} row{'' if len(rows) == 1 else 's'}:"]
            for number, row in enumerate(rows, 1):
                shown.append(f"    row {number}: " + " | ".join(cell_text(c) for c in row.findall(f"{W}tc")))
        else:
            notes = []
            style = styles.of_paragraph(block.element)
            if style != styles.default:
                notes.append(styles.name(style))
            numbering = block.element.find(f"{W}pPr/{W}numPr")
            if numbering is not None:
                level = numbering.find(f"{W}ilvl")
                value = level.get(f"{W}val", "0") if level is not None else "0"
                depth = int(value) if value.isdigit() else 0
                notes.append("list" if depth == 0 else f"list level {depth + 1}")
            if _ends_section(block.element):
                notes.append("ends a section")
            label = f" [{', '.join(notes)}]" if notes else ""
            shown = [f"#{block.id}{label} {paragraph_text(block.element, breaks=True) or '(empty)'}"]
        text = "\n".join(shown)
        if used + len(text) > MAX_OUTLINE and len(lines) > 1:
            lines.append(f"(cut short: call again with start={block.id} for the rest)")
            break
        lines.append(text)
        used += len(text) + 1
    return "\n".join(lines)


def _load(path: str, name: str) -> Document:
    try:
        return Document(path)
    except (zipfile.BadZipFile, KeyError, ValueError, ElementTree.ParseError, OSError) as exc:
        raise Refused(f"I couldn't read {name} as a Word document ({exc}).") from None


class WordOutline(Tool):
    name = "word_outline"
    description = "List a .docx's paragraphs (style, text) and tables with #ids; main text only. Use before edit_word."
    Args = OutlineArgs
    risk = RiskLevel.SAFE  # reads the file only
    category = "files"
    timeout_s = 30.0

    def __init__(self, folders: KnownFolders | None = None, memory: FileMemory | None = None) -> None:
        self._folders = folders or known_folder
        self.memory = memory if memory is not None else FILES

    async def describe_call(self, args: OutlineArgs, ctx: ToolContext) -> str:
        return f"read {args.path or f'result {args.number}'}"

    def _outline(self, args: OutlineArgs) -> str:
        path, name = resolve(args, self._folders, self.memory, ".docx", "Word")
        text = outline(_load(path, name), name, args.start)
        if _app_busy(path) == "open":
            text += (
                "\nNote: it's open in Word, so changes not saved there aren't in this list. Before editing it, the "
                "user needs to save and close it."
            )
        return text

    async def run(self, args: OutlineArgs, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult.success(await asyncio.to_thread(self._outline, args))
        except Refused as exc:
            return ToolResult.error(str(exc))


# --------------------------------------------------------------------------- edit_word


class EditArgs(FileArgs):
    edits: list[WordEdit] = Field(
        min_length=1,
        max_length=MAX_EDITS,
        description="Applied in order; all or none.",
    )


def _edit_phrase(edit: WordEdit, labels: dict[int, str]) -> str:
    label = labels.get(edit.id, f"#{edit.id}")
    text = _snippet(edit.text or "")
    if edit.action == "replace_text":
        return f'replace {label} with "{text}"'
    if edit.action == "insert_after":
        return f'add "{text}" after {label}'
    if edit.action == "insert_before":
        return f'add "{text}" before {label}'
    if edit.action == "delete":
        return f"delete {label}"
    return f'set row {edit.row}, column {edit.column} of table #{edit.id} to "{text}"'


class EditWord(Tool):
    name = "edit_word"
    description = (
        "Edit a .docx's paragraphs and tables without Word, keeping formatting. Call word_outline first. Won't "
        "touch a file open in Word. Saves in place; never uploads or submits."
    )
    Args = EditArgs
    risk = RiskLevel.CONFIRM  # rewrites the user's file, and deleted text can't be brought back from here
    category = "files"
    timeout_s = 60.0

    def __init__(
        self,
        folders: KnownFolders | None = None,
        memory: FileMemory | None = None,
        *,
        busy: Callable[[str], str | None] = _app_busy,
    ) -> None:
        self._folders = folders or known_folder
        self.memory = memory if memory is not None else FILES
        self._busy = busy

    def _labels(self, args: EditArgs) -> tuple[str, dict[int, str]]:
        """The file's name and the edited paragraphs' text, for the question the user is asked. Blocking."""
        path, name = resolve(args, self._folders, self.memory, ".docx", "Word")
        labels = {}
        try:
            document = Document(path)
            for edit in args.edits:
                block = document.block(edit.id)
                if not block.is_table and (text := paragraph_text(block.element, breaks=True).strip()):
                    labels[edit.id] = f'"{_snippet(text, 40)}"'
                elif block.is_table:
                    labels[edit.id] = f"table #{edit.id}"
        except Exception:  # only the wording of the question depends on it
            pass
        return name, labels

    async def describe_call(self, args: EditArgs, ctx: ToolContext) -> str:
        try:
            name, labels = await asyncio.to_thread(self._labels, args)
        except Refused:
            name, labels = args.path or f"result {args.number}", {}
        phrases = [_edit_phrase(edit, labels) for edit in args.edits]
        shown = "; ".join(phrases[:4]) + (f"; and {len(phrases) - 4} more" if len(phrases) > 4 else "")
        return f"edit {name}: {shown}"

    async def precheck(self, args: EditArgs, ctx: ToolContext) -> str | None:
        def check() -> str | None:
            try:
                path, name = resolve(args, self._folders, self.memory, ".docx", "Word")
            except Refused as exc:
                return str(exc)
            reason = self._busy(path)
            return busy_message(name, reason, "Word") if reason else None

        return await asyncio.to_thread(check)

    def _edit(self, args: EditArgs) -> ToolResult:
        try:
            path, name = resolve(args, self._folders, self.memory, ".docx", "Word")
        except Refused as exc:
            return ToolResult.error(str(exc))
        if reason := self._busy(path):
            return ToolResult.error(busy_message(name, reason, "Word"))
        try:
            document = _load(path, name)
        except Refused as exc:
            return ToolResult.error(str(exc))
        editor = Editor(document)
        done = []
        for number, edit in enumerate(args.edits, 1):
            try:
                done.append(editor.apply(edit))
            except EditError as exc:
                return ToolResult.error(f"Edit {number} can't be made: {exc} Nothing was changed.")
        if reason := self._busy(path):  # opened while the edits were being made
            return ToolResult.error(busy_message(name, reason, "Word"))
        try:
            document.save()
        except PermissionError:
            return ToolResult.error(busy_message(name, "locked", "Word"))
        except OSError as exc:
            return ToolResult.error(f"I couldn't save {name} ({exc.strerror or exc}). Nothing was changed.")
        lines = [f"Saved {name}:", *(f"- {line}" for line in done)]
        moved = ("insert_after", "insert_before", "delete")
        if any(
            edit.action in moved or (edit.action == "replace_text" and "\n" in (edit.text or "")) for edit in args.edits
        ):
            lines.append("Paragraphs were added or deleted, so the #ids changed: call word_outline before more edits.")
        return ToolResult.success("\n".join(lines), data={"edits": len(done)})

    async def run(self, args: EditArgs, ctx: ToolContext) -> ToolResult:
        return await asyncio.to_thread(self._edit, args)


TOOLS = [WordOutline, EditWord]
