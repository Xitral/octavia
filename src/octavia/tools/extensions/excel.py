"""The user's Excel files, edited as files rather than by clicking in Excel: ``excel_outline`` lists a workbook's
sheets and the cells of one (their values, and formulas with the value Excel last worked out), and ``edit_excel``
sets cells to values or formulas and adds rows below a sheet's last one.

Only the sheets that change are rewritten, with the workbook part (see ``_office`` for how the file is saved, and why
a file open in Excel is left alone). New text is written into its cell rather than Excel's shared list of strings,
so that list isn't rewritten. Excel works formulas out again when it next opens the file (``fullCalcOnLoad``), since
a changed value changes what the formulas that use it show; the list of formula cells it keeps for that
(``calcChain.xml``) is left out, as Excel calls a file damaged when that list names a cell without a formula, and it
rebuilds it. A cell whose formula Excel filled into other cells (a shared formula's first cell) or that's part of an
array formula isn't changed: the others would lose theirs.

Dates are numbers formatted as dates: they're shown as dates when their cell's format is one.
"""

from __future__ import annotations

import asyncio
import math
import posixpath
import re
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

MAX_ROW = 1_048_576
MAX_COLUMN = 16_384
MAX_TEXT = 32_767  # characters a cell holds
MAX_FORMULA = 8_192
_CELL = re.compile(r"\$?([A-Za-z]{1,3})\$?([0-9]{1,7})")
_COLUMN = re.compile(r"[A-Za-z]{1,3}")
_NUMBER = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
# Excel's built-in number formats that are dates or times.
_DATE_FORMATS = frozenset({*range(14, 23), *range(27, 37), 45, 46, 47, *range(50, 59)})
_FORMAT_LITERALS = re.compile(r'"[^"]*"|\\.|\[[^\]]*\]')
_VALUE_TAGS = {f"{S}f", f"{S}v", f"{S}is"}
# What comes before calcPr in a workbook part, in the order Excel requires.
_BEFORE_CALC = {f"{S}{tag}" for tag in ("sheets", "functionGroups", "externalReferences", "definedNames")}


# --------------------------------------------------------------------------- cell names


def column_number(letters: str) -> int:
    number = 0
    for letter in letters.upper():
        number = number * 26 + ord(letter) - ord("A") + 1
    return number


def column_letters(number: int) -> str:
    letters = ""
    while number:
        number, rest = divmod(number - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return letters


def cell_name(row: int, column: int) -> str:
    return f"{column_letters(column)}{row}"


def parse_cell(text: str) -> tuple[int, int]:
    """A cell's row and column from its name ("B4"). Raises EditError."""
    match = _CELL.fullmatch(text.strip())
    if match is None:
        raise EditError(f'"{text}" isn\'t a cell name like B4.')
    row, column = int(match.group(2)), column_number(match.group(1))
    if not (1 <= row <= MAX_ROW and 1 <= column <= MAX_COLUMN):
        raise EditError(f"{text} is outside a sheet's cells.")
    return row, column


@dataclass(frozen=True)
class Area:
    top: int
    left: int
    bottom: int
    right: int

    @classmethod
    def parse(cls, text: str) -> Area:
        """A range ("A1:D20") or one cell. Raises EditError."""
        first, _, last = text.partition(":")
        (top, left), (bottom, right) = parse_cell(first), parse_cell(last or first)
        return cls(min(top, bottom), min(left, right), max(top, bottom), max(left, right))

    def __contains__(self, place: tuple[int, int]) -> bool:
        row, column = place
        return self.top <= row <= self.bottom and self.left <= column <= self.right

    @property
    def single(self) -> bool:
        return self.top == self.bottom and self.left == self.right

    def __str__(self) -> str:
        first = cell_name(self.top, self.left)
        return first if self.single else f"{first}:{cell_name(self.bottom, self.right)}"


# --------------------------------------------------------------------------- reading


def _string(item: Element) -> str:
    """The text of a shared or inline string, rich or plain (without its phonetic guide)."""
    parts = []
    for child in item:
        if child.tag == f"{S}t":
            parts.append(child.text or "")
        elif child.tag == f"{S}r":
            parts.append(child.findtext(f"{S}t") or "")
    return "".join(parts)


def _is_date_format(code: str) -> bool:
    section = _FORMAT_LITERALS.sub("", code.split(";")[0])
    return re.search(r"[dmyhs]", section, re.IGNORECASE) is not None


def _date_styles(root: Element | None) -> frozenset[int]:
    """The cell styles (by index) whose number format is a date or time."""
    if root is None:
        return frozenset()
    custom = {}
    for number_format in root.iterfind(f"{S}numFmts/{S}numFmt"):
        if (format_id := number_format.get("numFmtId", "")).isdigit():
            custom[int(format_id)] = number_format.get("formatCode", "")
    dates = set()
    for index, style in enumerate(root.iterfind(f"{S}cellXfs/{S}xf")):
        format_id = style.get("numFmtId", "0")
        if not format_id.isdigit():
            continue
        number = int(format_id)
        if number in _DATE_FORMATS or (number in custom and _is_date_format(custom[number])):
            dates.add(index)
    return frozenset(dates)


def _shown_number(text: str, as_date: bool, date1904: bool) -> str:
    try:
        value = float(text)
    except ValueError:
        return text
    if as_date and math.isfinite(value) and value >= 0:
        base = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
        try:
            moment = base + timedelta(days=value)
        except OverflowError:
            return format(value, ".15g")
        if value == int(value):
            return moment.date().isoformat()
        if value < 1:
            return moment.strftime("%H:%M:%S")
        return moment.strftime("%Y-%m-%d %H:%M")
    return format(value, ".15g")


@dataclass
class Sheet:
    name: str
    part: str
    state: str
    root: Element
    namespaces: dict[str, str]
    changed: bool = False
    rows: dict[int, Element] = field(default_factory=dict)

    def __post_init__(self) -> None:
        data = self.root.find(f"{S}sheetData")
        if data is None:
            raise ValueError(f"sheet {self.name} has no cells")
        self.data = data
        # Row and cell names may be left out, meaning "the next one": they're written in, so every cell has one.
        previous = 0
        for row in data.findall(f"{S}row"):
            number = int(row.get("r") or previous + 1)
            row.set("r", str(number))
            previous = number
            self.rows[number] = row
            column = 0
            for cell in row.findall(f"{S}c"):
                column = parse_cell(cell.get("r", ""))[1] if cell.get("r") else column + 1
                cell.set("r", cell_name(number, column))
        self._read_formulas()

    @property
    def hidden(self) -> bool:
        return self.state in ("hidden", "veryHidden")

    def cells(self) -> Iterator[tuple[int, int, Element]]:
        for number in sorted(self.rows):
            for cell in self.rows[number].findall(f"{S}c"):
                yield (*parse_cell(cell.get("r", "")), cell)

    def used(self) -> Area | None:
        """The smallest range holding every cell with a value or formula."""
        places = [(row, column) for row, column, cell in self.cells() if _holds_something(cell)]
        if not places:
            return None
        rows, columns = [p[0] for p in places], [p[1] for p in places]
        return Area(min(rows), min(columns), max(rows), max(columns))

    def cell(self, row: int, column: int, *, create: bool) -> Element | None:
        row_element = self.rows.get(row)
        if row_element is None:
            if not create:
                return None
            row_element = Element(f"{S}row", {"r": str(row)})
            later = [number for number in self.rows if number > row]
            at = list(self.data).index(self.rows[min(later)]) if later else len(self.data)
            self.data.insert(at, row_element)
            self.rows[row] = row_element
        cells = row_element.findall(f"{S}c")
        for cell in cells:
            found = parse_cell(cell.get("r", ""))[1]
            if found == column:
                return cell
            if found > column:
                if not create:
                    return None
                return self._new_cell(row_element, list(row_element).index(cell), row, column)
        if not create:
            return None
        at = list(row_element).index(cells[-1]) + 1 if cells else 0
        return self._new_cell(row_element, at, row, column)

    @staticmethod
    def _new_cell(row_element: Element, at: int, row: int, column: int) -> Element:
        cell = Element(f"{S}c", {"r": cell_name(row, column)})
        row_element.insert(at, cell)
        row_element.attrib.pop("spans", None)  # an optional hint of which columns the row uses, now out of date
        return cell

    def _read_formulas(self) -> None:
        """Where the formulas that cover several cells are, and which cell each shared formula is written in."""
        self._spread: list[tuple[str, Area, tuple[int, int]]] = []
        self._shared: dict[str, str] = {}
        for row, column, cell in self.cells():
            formula = cell.find(f"{S}f")
            if formula is None or formula.get("t") not in ("shared", "array"):
                continue
            if formula.get("t") == "shared" and formula.text:
                self._shared.setdefault(formula.get("si", ""), cell.get("r", ""))
            try:
                area = Area.parse(formula.get("ref", ""))
            except EditError:
                continue
            if not area.single:
                self._spread.append((formula.get("t", ""), area, (row, column)))

    def shared_source(self, group: str) -> str | None:
        """The cell a shared formula is written in."""
        return self._shared.get(group)

    def multi_cell_formula(self, row: int, column: int) -> str | None:
        """Why the cell can't be changed on its own: it starts a formula Excel filled into other cells, or it's part
        of an array formula. None when it can."""
        for kind, area, start in self._spread:
            if kind == "shared" and start == (row, column):
                return f"its formula is the one Excel filled into {area}"
            if kind == "array" and (row, column) in area:
                return f"it's part of an array formula over {area}"
        return None

    def update_dimension(self) -> None:
        dimension = self.root.find(f"{S}dimension")
        places = [(row, column) for row, column, _ in self.cells()]
        if dimension is None or not places:
            return
        rows, columns = [p[0] for p in places], [p[1] for p in places]
        dimension.set("ref", str(Area(min(rows), min(columns), max(rows), max(columns))))


def _holds_something(cell: Element) -> bool:
    return any(child.tag in _VALUE_TAGS for child in cell)


class Book:
    """A workbook, read: its worksheets in tab order, its shared strings and which styles are dates. Blocking."""

    def __init__(self, path: str) -> None:
        self.path = path
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            self.part = _related(_rels(archive, names, ""), "officeDocument") or "xl/workbook.xml"
            self.root, self.namespaces = _parse(_read(archive, self.part))
            rels = _rels(archive, names, self.part)
            strings_part = _related(rels, "sharedStrings")
            self.strings: list[str] = []
            if strings_part in names:
                self.strings = [_string(item) for item in _parse(_read(archive, strings_part))[0].iterfind(f"{S}si")]
            styles_part = _related(rels, "styles")
            self.dates = _date_styles(_parse(_read(archive, styles_part))[0] if styles_part in names else None)
            properties = self.root.find(f"{S}workbookPr")
            self.date1904 = properties is not None and properties.get("date1904") in ("1", "true")
            self.sheets: list[Sheet] = []
            for sheet in self.root.iterfind(f"{S}sheets/{S}sheet"):
                kind, part = rels.get(sheet.get(f"{R}id", ""), ("", ""))
                if not kind.endswith("/worksheet") or part not in names:
                    continue  # a chart sheet, or missing
                root, namespaces = _parse(_read(archive, part))
                self.sheets.append(Sheet(sheet.get("name", ""), part, sheet.get("state", "visible"), root, namespaces))
            # The calculation chain, to leave out when saving: its part, its relationship's id, and the two parts
            # that name it.
            self.calc_chain = next(
                ((rel_id, target) for rel_id, (kind, target) in rels.items() if kind.endswith("/calcChain")), None
            )
            folder, base = posixpath.split(self.part)
            self.rels_part = posixpath.join(folder, "_rels", f"{base}.rels")
            self.rels_xml = _read(archive, self.rels_part) if self.calc_chain else b""
            self.types_xml = _read(archive, "[Content_Types].xml") if self.calc_chain else b""

    def sheet(self, name: str | None) -> Sheet:
        listing = ", ".join(f'"{sheet.name}"' for sheet in self.sheets) or "none"
        if name is None:
            if len(self.sheets) == 1:
                return self.sheets[0]
            raise EditError(f"Name the sheet: the workbook has {listing}.")
        for sheet in self.sheets:
            if sheet.name.casefold() == name.strip().casefold():
                return sheet
        raise EditError(f'There\'s no sheet "{name}"; the workbook has {listing}.')

    def shown(self, sheet: Sheet, cell: Element) -> str | None:
        """What the cell holds, as Excel shows it (a formula as "=formula → its value"), or None when it's empty."""
        kind = cell.get("t", "n")
        value = cell.findtext(f"{S}v")
        text: str | None
        if kind == "inlineStr":
            item = cell.find(f"{S}is")
            text = _string(item) if item is not None else ""
        elif value is None:
            text = None
        elif kind == "s":
            text = self.strings[int(value)] if value.isdigit() and int(value) < len(self.strings) else ""
        elif kind == "b":
            text = "TRUE" if value.strip() == "1" else "FALSE"
        elif kind in ("e", "str"):
            text = value
        else:
            style = cell.get("s", "0")
            text = _shown_number(value, style.isdigit() and int(style) in self.dates, self.date1904)
        formula = cell.find(f"{S}f")
        if formula is None:
            return text
        if formula.text:
            written = f"={formula.text}"
        elif formula.get("t") == "shared":
            source = sheet.shared_source(formula.get("si", ""))
            written = f"(formula filled from {source}'s)" if source else "(formula filled from another cell's)"
        else:
            written = "(formula)"
        return written if text is None else f"{written} → {text}"

    def save(self) -> None:
        changed_sheets = [sheet for sheet in self.sheets if sheet.changed]
        if not changed_sheets:
            return
        changed = {}
        for sheet in changed_sheets:
            sheet.update_dimension()
            changed[sheet.part] = serialize(sheet.root, sheet.namespaces)
        calculation = self.root.find(f"{S}calcPr")
        if calculation is None:
            calculation = Element(f"{S}calcPr")
            at = max(i for i, child in enumerate(self.root) if child.tag in _BEFORE_CALC) + 1
            self.root.insert(at, calculation)
        calculation.set("fullCalcOnLoad", "1")
        changed[self.part] = serialize(self.root, self.namespaces)
        removed: frozenset[str] = frozenset()
        if self.calc_chain is not None:
            rel_id, target = self.calc_chain
            relationship = rf'<Relationship\b(?=[^>]*\bId="{re.escape(rel_id)}")[^>]*/>'
            override = rf'<Override\b(?=[^>]*\bPartName="/{re.escape(target)}")[^>]*/>'
            changed[self.rels_part] = re.sub(relationship.encode(), b"", self.rels_xml)
            changed["[Content_Types].xml"] = re.sub(override.encode(), b"", self.types_xml)
            removed = frozenset({target})
        save_parts(self.path, changed, removed)


def _load(path: str, name: str) -> Book:
    try:
        return Book(path)
    except (zipfile.BadZipFile, KeyError, ValueError, EditError, ElementTree.ParseError, OSError) as exc:
        raise Refused(f"I couldn't read {name} as an Excel workbook ({exc}).") from None


def _app_busy(path: str) -> str | None:
    return busy_reason(path)


# --------------------------------------------------------------------------- excel_outline


class OutlineArgs(FileArgs):
    sheet: str | None = Field(default=None, max_length=100, description="Default: first visible.")
    range: str | None = Field(default=None, max_length=30, description="Only these cells, e.g. 'A1:F40'.")


def outline(book: Book, name: str, sheet_name: str | None, area_text: str | None) -> str:
    count = len(book.sheets)
    lines = [f"{name}: {count} sheet{'' if count == 1 else 's'}."]
    for sheet in book.sheets:
        used = sheet.used()
        lines.append(f'- "{sheet.name}"{" (hidden)" if sheet.hidden else ""}: {used or "empty"}')
    if not book.sheets:
        return "\n".join(lines)
    if sheet_name is not None:
        sheet = book.sheet(sheet_name)
    else:
        sheet = next((each for each in book.sheets if not each.hidden), book.sheets[0])
    area = Area.parse(area_text) if area_text else sheet.used()
    if area is None:
        lines.append(f'Sheet "{sheet.name}" is empty.')
        return "\n".join(lines)
    lines.append(
        f'Sheet "{sheet.name}", {area}, a line per row. Formulas show as =formula → the value Excel last worked out.'
    )
    used = sum(len(line) + 1 for line in lines)
    for number in sorted(n for n in sheet.rows if area.top <= n <= area.bottom):
        shown = []
        for cell in sheet.rows[number].findall(f"{S}c"):
            column = parse_cell(cell.get("r", ""))[1]
            if area.left <= column <= area.right and (value := book.shown(sheet, cell)) is not None:
                shown.append(f"{cell.get('r')}: {value.replace(chr(10), ' / ')}")
        if not shown:
            continue
        line = f"Row {number}: " + " | ".join(shown)
        if used + len(line) > MAX_OUTLINE:
            lines.append(f"(cut short: ask for a range starting at row {number} for the rest)")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


class ExcelOutline(Tool):
    name = "excel_outline"
    description = (
        "List an .xlsx workbook's sheets and one sheet's cells and values, a line per row. Use before edit_excel."
    )
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
        path, name = resolve(args, self._folders, self.memory, ".xlsx", "Excel")
        book = _load(path, name)
        try:
            text = outline(book, name, args.sheet, args.range)
        except EditError as exc:
            raise Refused(str(exc)) from None
        if _app_busy(path) == "open":
            text += (
                "\nNote: it's open in Excel, so changes not saved there aren't in this list. Before editing it, the "
                "user needs to save and close it."
            )
        return text

    async def run(self, args: OutlineArgs, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult.success(await asyncio.to_thread(self._outline, args))
        except Refused as exc:
            return ToolResult.error(str(exc))


# --------------------------------------------------------------------------- edit_excel


Kind = Literal["clear", "formula", "number", "bool", "text"]


def typed(value: str, as_text: bool) -> tuple[Kind, str]:
    """What a value is written as, as Excel takes what's typed into a cell: a formula, a number, TRUE or FALSE, or
    text; and its text as stored."""
    if value == "":
        return "clear", ""
    if as_text:
        return "text", value
    if value.startswith("=") and len(value) > 1:
        return "formula", value[1:]
    stripped = value.strip()
    if _NUMBER.fullmatch(stripped):
        number = float(stripped)
        if math.isfinite(number):
            whole = number == int(number) and abs(number) < 1e15
            return "number", str(int(number)) if whole else repr(number)
    if stripped.upper() in ("TRUE", "FALSE"):
        return "bool", "1" if stripped.upper() == "TRUE" else "0"
    return "text", value


def write(sheet: Sheet, row: int, column: int, value: str, as_text: bool, style: str | None = None) -> str:
    """Puts a value in a cell, keeping its formatting; what it holds now, as it'd be shown. Raises EditError."""
    kind, text = typed(value, as_text)
    name = cell_name(row, column)
    if kind == "formula" and len(text) > MAX_FORMULA:
        raise EditError(f"The formula for {name} is longer than Excel allows.")
    if reason := sheet.multi_cell_formula(row, column):
        raise EditError(f"I can't change {sheet.name}!{name} on its own: {reason}. That needs Excel itself.")
    cell = sheet.cell(row, column, create=kind != "clear" or style is not None)
    if cell is None:
        return "empty"
    for child in [c for c in cell if c.tag in _VALUE_TAGS]:
        cell.remove(child)
    for attribute in ("t", "cm", "vm"):  # its kind, and metadata about the old value
        cell.attrib.pop(attribute, None)
    if style is not None:
        cell.set("s", style)
    pieces: list[Element] = []
    if kind == "formula":
        formula = Element(f"{S}f")
        formula.text = text
        pieces.append(formula)
    elif kind in ("number", "bool"):
        if kind == "bool":
            cell.set("t", "b")
        number = Element(f"{S}v")
        number.text = text
        pieces.append(number)
    elif kind == "text":
        cell.set("t", "inlineStr")
        item = Element(f"{S}is")
        written = ElementTree.SubElement(item, f"{S}t")
        written.text = text
        if text != text.strip():
            written.set(XML_SPACE, "preserve")
        pieces.append(item)
    for offset, piece in enumerate(pieces):
        cell.insert(offset, piece)
    sheet.changed = True
    return {"clear": "empty", "formula": f"={text}", "bool": "TRUE" if text == "1" else "FALSE"}.get(kind, text)


class CellEdit(ToolArgs):
    action: Literal["set", "add_row"] = Field(
        description="set: cell + value. add_row: values, below the last used row."
    )
    sheet: str | None = Field(default=None, max_length=100, description="Needed if there are several.")
    cell: str | None = Field(default=None, max_length=12, description="e.g. 'B4'.")
    value: str | None = Field(
        default=None,
        max_length=MAX_TEXT,
        description="As typed into Excel, e.g. '=SUM(B2:B9)'. '' clears it.",
    )
    values: list[str] | None = Field(default=None, min_length=1, max_length=200, description="Left to right.")
    start_column: str | None = Field(default=None, max_length=3, description="e.g. 'A'. Default: first used.")
    as_text: bool = Field(default=False, description="Keep values like '007' as text.")

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if self.action == "set":
            if self.cell is None or self.value is None:
                raise ValueError("set needs cell and value")
            if self.values is not None or self.start_column is not None:
                raise ValueError("values and start_column are only for add_row")
            try:
                parse_cell(self.cell)
            except EditError as exc:
                raise ValueError(str(exc)) from None
            self.value = clean_text(self.value)
        else:
            if self.values is None:
                raise ValueError("add_row needs values")
            if self.cell is not None or self.value is not None:
                raise ValueError("cell and value are only for set")
            if self.start_column is not None and not _COLUMN.fullmatch(self.start_column):
                raise ValueError("start_column is a column's letters, like A")
            self.values = [clean_text(each) for each in self.values]
        return self


def apply_edit(book: Book, edit: CellEdit) -> str:
    """Makes one edit in memory; what was done. Raises EditError."""
    sheet = book.sheet(edit.sheet)
    if edit.action == "set":
        assert edit.cell is not None and edit.value is not None
        row, column = parse_cell(edit.cell)
        now = write(sheet, row, column, edit.value, edit.as_text)
        return f'set {sheet.name}!{cell_name(row, column)} to "{now}"'
    assert edit.values is not None
    used = sheet.used()
    row = used.bottom + 1 if used else 1
    if row > MAX_ROW:
        raise EditError(f'Sheet "{sheet.name}" has no rows left.')
    if edit.start_column is not None:
        left = column_number(edit.start_column)
    else:
        left = used.left if used else 1
    if left + len(edit.values) - 1 > MAX_COLUMN:
        raise EditError("That row goes past the sheet's last column.")
    above = sheet.rows.get(row - 1)
    for offset, value in enumerate(edit.values):
        column = left + offset
        source = sheet.cell(row - 1, column, create=False) if above is not None else None
        style = source.get("s") if source is not None else None
        write(sheet, row, column, value, edit.as_text, style)
    if above is not None and (new_row := sheet.rows.get(row)) is not None:
        for attribute in ("s", "customFormat"):
            if attribute in above.attrib:
                new_row.set(attribute, above.get(attribute, ""))
    sheet.changed = True
    last = cell_name(row, left + len(edit.values) - 1)
    return f"added row {row} to {sheet.name} ({cell_name(row, left)}:{last})"


class EditArgs(FileArgs):
    edits: list[CellEdit] = Field(min_length=1, max_length=MAX_EDITS, description="Applied in order; all or none.")


def _edit_phrase(edit: CellEdit, sheets: int) -> str:
    where = f"{edit.sheet}!" if edit.sheet and sheets > 1 else ""
    if edit.action == "set":
        value = edit.value or ""
        if not value:
            return f"clear {where}{(edit.cell or '').upper()}"
        short = value if len(value) <= 40 else value[:37] + "..."
        return f'set {where}{(edit.cell or "").upper()} to "{short}"'
    shown = ", ".join(f'"{v}"' for v in (edit.values or [])[:4]) + (", ..." if len(edit.values or []) > 4 else "")
    return f"add a row ({shown})" + (f" to {edit.sheet}" if edit.sheet and sheets > 1 else "")


class EditExcel(Tool):
    name = "edit_excel"
    description = (
        "Edit an .xlsx's cells and rows without Excel, keeping formatting. Call excel_outline first. Won't touch a "
        "file open in Excel. Saves in place; never uploads or submits."
    )
    Args = EditArgs
    risk = RiskLevel.CONFIRM  # rewrites the user's file, and overwritten values can't be brought back from here
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

    def _name(self, args: EditArgs) -> tuple[str, int]:
        """The file's name and how many sheets it has, for the question the user is asked. Blocking."""
        path, name = resolve(args, self._folders, self.memory, ".xlsx", "Excel")
        try:
            return name, len(Book(path).sheets)
        except Exception:  # only the wording of the question depends on it
            return name, 2

    async def describe_call(self, args: EditArgs, ctx: ToolContext) -> str:
        try:
            name, sheets = await asyncio.to_thread(self._name, args)
        except Refused:
            name, sheets = args.path or f"result {args.number}", 2
        phrases = [_edit_phrase(edit, sheets) for edit in args.edits]
        shown = "; ".join(phrases[:4]) + (f"; and {len(phrases) - 4} more" if len(phrases) > 4 else "")
        return f"edit {name}: {shown}"

    async def precheck(self, args: EditArgs, ctx: ToolContext) -> str | None:
        def check() -> str | None:
            try:
                path, name = resolve(args, self._folders, self.memory, ".xlsx", "Excel")
            except Refused as exc:
                return str(exc)
            reason = self._busy(path)
            return busy_message(name, reason, "Excel") if reason else None

        return await asyncio.to_thread(check)

    def _edit(self, args: EditArgs) -> ToolResult:
        try:
            path, name = resolve(args, self._folders, self.memory, ".xlsx", "Excel")
        except Refused as exc:
            return ToolResult.error(str(exc))
        if reason := self._busy(path):
            return ToolResult.error(busy_message(name, reason, "Excel"))
        try:
            book = _load(path, name)
        except Refused as exc:
            return ToolResult.error(str(exc))
        done = []
        for number, edit in enumerate(args.edits, 1):
            try:
                done.append(apply_edit(book, edit))
            except EditError as exc:
                return ToolResult.error(f"Edit {number} can't be made: {exc} Nothing was changed.")
        if reason := self._busy(path):  # opened while the edits were being made
            return ToolResult.error(busy_message(name, reason, "Excel"))
        try:
            book.save()
        except PermissionError:
            return ToolResult.error(busy_message(name, "locked", "Excel"))
        except OSError as exc:
            return ToolResult.error(f"I couldn't save {name} ({exc.strerror or exc}). Nothing was changed.")
        lines = [f"Saved {name}:", *(f"- {line}" for line in done)]
        lines.append("Formulas' values are worked out by Excel when it next opens the file.")
        return ToolResult.success("\n".join(lines), data={"edits": len(done)})

    async def run(self, args: EditArgs, ctx: ToolContext) -> ToolResult:
        return await asyncio.to_thread(self._edit, args)


TOOLS = [ExcelOutline, EditExcel]
