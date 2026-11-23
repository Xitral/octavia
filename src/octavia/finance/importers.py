"""Statements the user downloads from their bank, read into plain rows: CSV (any bank's columns), OFX, QFX and QBO.

A CSV's columns are found by their names (``_COLUMNS``): a date, a description (or a payee or a name), and an amount
(or a debit and a credit column, or an amount with a "debit"/"credit" type). Cash App's export has its own columns
(transaction type, net amount, the name of who was paid, notes, status), recognised by name too; a failed or
canceled one is left out. OFX (QFX and QBO are OFX) is read by its tags: the account, every transaction (with the
bank's own id, ``FITID``) and the ledger balance.

Which account a file is: the institution from its words (the file name, Cash App's columns, OFX's ``<ORG>``), the
kind from the OFX account type or the institution (USAA checking, Cash App, a loan servicer). Nothing here keeps a
whole account number: OFX's ``ACCTID`` is cut to its last 4.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from octavia.finance.privacy import last4, mask

EXTENSIONS = (".csv", ".ofx", ".qfx", ".qbo")
MAX_BYTES = 20 * 1024 * 1024


class ImportError_(ValueError):
    """A file that can't be read as a statement (says why)."""


@dataclass
class Row:
    day: date
    cents: int  # money in > 0, money out < 0
    description: str
    category: str = ""
    memo: str = ""
    hint: str = ""  # the statement's own type for it ("Sent P2P", "DEBIT", "Cash Card Debit")
    pending: bool = False
    bank_id: str = ""  # OFX FITID / Cash App's transaction id: the same transaction in the next export


@dataclass
class Statement:
    institution: str  # "usaa", "cash_app", "mohela", or "" when the file doesn't say
    kind: str  # "checking", "savings", "cash", "credit", "loan", "investing", or ""
    last4: str = ""
    rows: list[Row] = field(default_factory=list)
    balance_cents: int | None = None
    balance_day: date | None = None
    source: str = ""  # "csv" / "ofx"


# ----------------------------------------------------------------------------------------------- shared parsing

_AMOUNT = re.compile(r"^\(?\s*([+\-−]?)\s*\$?\s*([+\-−]?)\s*([\d,]*\.?\d+)\s*\)?\s*(cr|dr|-)?$", re.I)


def parse_amount(text: str) -> int | None:
    """Cents from "$1,234.56", "-$12.00", "(12.00)", "+5", "12.00 CR". None if it isn't an amount."""
    raw = str(text or "").strip().replace("USD", "").replace("US$", "").strip()
    if not raw:
        return None
    m = _AMOUNT.match(raw)
    if not m:
        return None
    sign = -1 if (m.group(1) in ("-", "−") or m.group(2) in ("-", "−") or raw.startswith("(")) else 1
    if (m.group(4) or "").lower() in ("dr", "-"):  # "12.00 DR", "12.34-"
        sign = -1
    try:
        value = float(m.group(3).replace(",", ""))
    except ValueError:
        return None
    return sign * round(value * 100)


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%m-%d-%Y")


def parse_date(text: str) -> date | None:
    """A date from the formats statements use ("2026-11-07", "11/07/2026", "11/7/26", "Nov 7, 2026", and Cash
    App's "2026-11-07 14:03:22 EST")."""
    raw = " ".join(str(text or "").split())
    if not raw:
        return None
    raw = re.sub(r"\s+[A-Z]{2,4}$", "", raw)  # a time zone's letters
    candidates = [raw, raw.split(" ")[0], raw.split("T")[0]]
    for value in candidates:
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
    m = re.match(r"^(\d{4})(\d{2})(\d{2})", raw)  # OFX: 20261107120000[-5:EST]
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def bank_id(raw: str) -> str:
    """The bank's own id for a transaction, kept as a hash (some banks build it from the account number)."""
    raw = str(raw or "").strip()
    return hashlib.sha256(raw.encode()).hexdigest()[:20] if raw else ""


def institution_of(*texts: str) -> str:
    joined = " ".join(t.lower() for t in texts if t)
    if re.search(r"cash[\s_-]*app|square", joined):
        return "cash_app"
    if "usaa" in joined:
        return "usaa"
    if "mohela" in joined:
        return "mohela"
    return ""


DEFAULT_KIND = {"usaa": "checking", "cash_app": "cash", "mohela": "loan"}


# ------------------------------------------------------------------------------------------------------- CSV

_COLUMNS = {
    "date": ("date", "posted date", "transaction date", "posting date", "posted", "trans date", "date posted"),
    "description": (
        "description",
        "payee",
        "name of sender/receiver",
        "merchant",
        "name",
        "original description",
        "transaction description",
        "details",
    ),
    "amount": ("net amount", "amount", "transaction amount", "amount (usd)"),
    "debit": ("debit", "withdrawal", "withdrawals", "debit amount", "money out"),
    "credit": ("credit", "deposit", "deposits", "credit amount", "money in"),
    "category": ("category",),
    "memo": ("notes", "memo", "note"),
    "type": ("transaction type", "type", "debit/credit"),
    "status": ("status",),
    "balance": ("balance", "running balance", "running bal.", "available balance"),
    "id": ("transaction id", "id", "reference", "reference number"),
}
_SKIP_STATUS = re.compile(r"\b(?:failed|canceled|cancelled|declined|refused)\b", re.I)


def _find_columns(header: list[str]) -> dict[str, int]:
    names = [" ".join(h.strip().lower().split()) for h in header]
    found: dict[str, int] = {}
    for key, options in _COLUMNS.items():
        for option in options:  # the first name in the list wins, so "description" beats "original description"
            if option in names and names.index(option) not in found.values():
                found[key] = names.index(option)
                break
    return found


def read_csv(text: str, name: str = "") -> Statement:
    rows = list(csv.reader(io.StringIO(text)))
    header_at, columns = -1, {}
    for i, row in enumerate(rows[:15]):  # some banks put a title or the account above the header
        found = _find_columns(row)
        if "date" in found and ("amount" in found or "debit" in found or "credit" in found):
            header_at, columns = i, found
            break
    if header_at < 0:
        raise ImportError_("its columns don't say which is the date and which is the amount")
    header = rows[header_at]
    institution = institution_of(name, " ".join(header))
    statement = Statement(institution, DEFAULT_KIND.get(institution, ""), source="csv")
    balances: list[tuple[date, int]] = []  # (day, running balance), in the file's order

    def cell(row: list[str], key: str) -> str:
        i = columns.get(key)
        return row[i].strip() if i is not None and i < len(row) else ""

    for row in rows[header_at + 1 :]:
        if not any(c.strip() for c in row):
            continue
        day = parse_date(cell(row, "date"))
        if day is None:
            continue
        status = cell(row, "status")
        if status and _SKIP_STATUS.search(status):
            continue
        cents = parse_amount(cell(row, "amount")) if "amount" in columns else None
        kind = cell(row, "type")
        if cents is None:
            debit, credit = parse_amount(cell(row, "debit")), parse_amount(cell(row, "credit"))
            if debit:
                cents = -abs(debit)
            elif credit:
                cents = abs(credit)
        elif re.fullmatch(r"(?i)debit|dr|withdrawal", kind):
            cents = -abs(cents)
        elif re.fullmatch(r"(?i)credit|cr|deposit", kind):
            cents = abs(cents)
        if cents is None or cents == 0:
            continue
        description = cell(row, "description") or kind or "(no description)"
        statement.rows.append(
            Row(
                day=day,
                cents=cents,
                description=mask(description)[:200],
                category=mask(cell(row, "category"))[:60],
                memo=mask(cell(row, "memo"))[:200],
                hint=mask(kind)[:60],
                pending=bool(re.search(r"(?i)pending", status)),
                bank_id=bank_id(cell(row, "id")),
            )
        )
        balance = parse_amount(cell(row, "balance")) if "balance" in columns else None
        if balance is not None:
            balances.append((day, balance))
    if balances:
        newest_first = balances[0][0] > balances[-1][0]
        latest = max(day for day, _ in balances)
        on_latest = [b for b in balances if b[0] == latest]
        # the balance after the latest day's last transaction: its first row in a newest-first file, else its last
        statement.balance_day, statement.balance_cents = on_latest[0] if newest_first else on_latest[-1]
    return statement


# ------------------------------------------------------------------------------------------------------- OFX

_TAG = re.compile(r"<([A-Z0-9.]+)>([^<\r\n]*)", re.I)
_ACCOUNT_KINDS = {"CHECKING": "checking", "SAVINGS": "savings", "MONEYMRKT": "savings", "CREDITLINE": "credit"}


def _tags(block: str) -> dict[str, str]:
    return {m.group(1).upper(): m.group(2).strip() for m in _TAG.finditer(block)}


def read_ofx(text: str, name: str = "") -> Statement:
    if "<OFX>" not in text.upper():
        raise ImportError_("it isn't an OFX file (no <OFX> in it)")
    upper = text.upper()
    head = _tags(text[: upper.find("<BANKTRANLIST>") if "<BANKTRANLIST>" in upper else len(text)])
    institution = institution_of(name, head.get("ORG", ""), head.get("FID", ""))
    kind = _ACCOUNT_KINDS.get(head.get("ACCTTYPE", "").upper(), "")
    if "<CCSTMTRS>" in upper:
        kind = "credit"
    statement = Statement(
        institution, kind or DEFAULT_KIND.get(institution, ""), last4(head.get("ACCTID")), source="ofx"
    )
    for block in re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|(?=</BANKTRANLIST>))", text, re.I | re.S):
        tags = _tags(block)
        day = parse_date(tags.get("DTPOSTED", ""))
        cents = parse_amount(tags.get("TRNAMT", ""))
        if day is None or not cents:
            continue
        statement.rows.append(
            Row(
                day=day,
                cents=cents,
                description=mask(tags.get("NAME") or tags.get("PAYEE") or tags.get("MEMO") or "(no description)")[:200],
                memo=mask(tags.get("MEMO", "")) if tags.get("NAME") else "",
                hint=mask(tags.get("TRNTYPE", ""))[:30],
                bank_id=bank_id(tags.get("FITID", "")),
            )
        )
    ledger = re.search(r"<LEDGERBAL>(.*?)(?:</LEDGERBAL>|<AVAILBAL>|$)", text, re.I | re.S)
    if ledger:
        tags = _tags(ledger.group(1))
        cents, day = parse_amount(tags.get("BALAMT", "")), parse_date(tags.get("DTASOF", ""))
        if cents is not None:
            statement.balance_cents, statement.balance_day = (
                cents,
                day or max((r.day for r in statement.rows), default=None),
            )
    return statement


# ------------------------------------------------------------------------------------------------------ files


def read_ofx_all(text: str, name: str = "") -> list[Statement]:
    """Every account's statement in an OFX file (one download can hold checking and savings)."""
    starts = [m.start() for m in re.finditer(r"<(?:CC)?STMTRS>", text, re.I)]
    if len(starts) <= 1:
        return [read_ofx(text, name)]
    head = text[: starts[0]]  # the sign-on part: the bank's <ORG>
    ends = [*starts[1:], len(text)]
    return [read_ofx(head + text[a:b] + "</OFX>", name) for a, b in zip(starts, ends, strict=True)]


def read_statements(path: Path) -> list[Statement]:
    """Every statement in ``path``: one for a CSV, one per account for OFX. Raises ``ImportError_``."""
    path = Path(path)
    text = _text(path)
    if path.suffix.lower() in (".ofx", ".qfx", ".qbo") or "<OFX>" in text[:4000].upper():
        return read_ofx_all(text, path.name)
    return [read_file(path, text)]


def _text(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ImportError_("it's too big for a statement")
        raw = path.read_bytes()
    except OSError as exc:
        raise ImportError_(f"it couldn't be opened ({exc.strerror or exc})") from exc
    return raw.decode("utf-8-sig", errors="replace")


def read_file(path: Path, text: str | None = None) -> Statement:
    """A statement from ``path`` (by its extension, else its content). Raises ``ImportError_``."""
    path = Path(path)
    text = _text(path) if text is None else text
    if path.suffix.lower() in (".ofx", ".qfx", ".qbo") or "<OFX>" in text[:4000].upper():
        return read_ofx(text, path.name)
    if path.suffix.lower() == ".csv" or "," in text[:2000]:
        return read_csv(text, path.name)
    raise ImportError_("it isn't a CSV, OFX, QFX or QBO file")


def statement_files(folder: Path) -> Iterable[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS)
