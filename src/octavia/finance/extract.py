"""A bank page's text, read for its money: transactions, a balance, holdings, a credit score, a loan's terms.

Pages change their layout without notice, so this reads them the way a person skims: lines with one amount are
transactions (the words on the line, or the lines just above it, say who; a date on it or among those lines, else
the last date heading above it, says when; a "+" or words like "received" say money came in), and labelled amounts
are the rest ("Cash balance $120.50", "Credit score 712", "Monthly payment $187.22", "Due date Nov 28"). A line
that's a summary (a balance, a total, a limit) is never a transaction, and a line read once (a label's value, a
transaction's words) isn't read again. What it finds is kept with the page as its source, so a statement of the same
transactions is matched to it and not counted twice (``store.add_rows``).

It never guesses an amount: every amount kept is written on the page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from octavia.finance.importers import Row, parse_amount
from octavia.finance.privacy import mask

_MONEY = re.compile(r"([+\-−]?\s?\$\s?[+\-−]?\d[\d,]*\.\d{2})")
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_DATE = re.compile(
    rf"\b(?:(?:{_MONTHS})[a-z]*\.?\s+\d{{1,2}}(?:,?\s+\d{{4}})?|\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?|today|yesterday)\b",
    re.I,
)
_SUMMARY = re.compile(
    r"\b(?:balance|total|available|limit|minimum|score|apr|interest\s+rate|statement|portfolio|"
    r"credit\s+line|payment\s+due|amount\s+due|monthly\s+payment|principal|pay\s+off|payoff|value)\b",
    re.I,
)
_IN_WORDS = re.compile(
    r"\b(?:received|deposit(?:ed)?|cash\s+in|refund(?:ed)?|paid\s+you|payroll|direct\s+dep|"
    r"interest\s+earned|sold|added)\b",
    re.I,
)
_SCORE_LABEL = re.compile(r"\b(?:credit\s+score|vantagescore|fico)\b", re.I)
_SCORE = re.compile(r"(?<![\d$.,])([3-8]\d{2})(?![\d.,%])")
_LOAN_BALANCE = re.compile(r"\b(?:current|outstanding|total|loan|principal)\s+balance\b", re.I)
_LOAN_PAYMENT = re.compile(r"\b(?:monthly\s+payment|amount\s+due|payment\s+amount|current\s+amount\s+due)\b", re.I)
_LOAN_DUE = re.compile(r"\b(?:due\s+date|payment\s+due|next\s+payment)\b", re.I)
_HOLDING = re.compile(rf"(stocks|investing|bitcoin|savings)(?:\s+balance)?\s*(?:{_MONEY.pattern})?", re.I)
_BALANCE = re.compile(
    r"\b(?:cash\s+balance|available\s+balance|current\s+balance|account\s+balance)\b|^balance\b", re.I
)
# An account's tile on a summary page ("Classic Checking …1234 $1,234.56", "As of Nov 7"): a balance, never a
# transaction.
_ACCOUNT = re.compile(r"\b(?:checking|savings|account|as\s+of|ending\s+in)\b|…\d{4}|\*{2,}\d{4}|x{2,}\d{4}", re.I)
_LETTERS = re.compile(r"[A-Za-z]{2,}")
_PENDING = re.compile(r"\bpending\b", re.I)


@dataclass
class PageMoney:
    rows: list[Row] = field(default_factory=list)
    balance_cents: int | None = None
    holdings: dict[str, int] = field(default_factory=dict)  # "stocks", "savings", "bitcoin"
    score: int | None = None
    score_model: str = ""
    loan_balance_cents: int | None = None
    loan_payment_cents: int | None = None
    loan_due: date | None = None

    @property
    def empty(self) -> bool:
        return not (self.rows or self.balance_cents is not None or self.holdings or self.score is not None
                    or self.loan_balance_cents is not None)  # fmt: skip


def page_date(text: str, today: date) -> date | None:
    found = _DATE.search(text)
    if not found:
        return None
    word = found.group(0).lower()
    if word == "today":
        return today
    if word == "yesterday":
        return today - timedelta(days=1)
    m = re.match(rf"({_MONTHS})[a-z]*\.?\s+(\d{{1,2}})(?:,?\s+(\d{{4}}))?", word)
    try:
        if m:
            month = [x for x in _MONTHS.split("|") if x != "sept"].index(m.group(1)[:3]) + 1
            year = int(m.group(3)) if m.group(3) else today.year
            day = date(year, month, int(m.group(2)))
        else:
            parts = [int(x) for x in word.split("/")]
            year = parts[2] if len(parts) > 2 else today.year
            year = year + 2000 if year < 100 else year
            day = date(year, parts[0], parts[1])
    except (ValueError, IndexError):
        return None
    if (not m or not m.group(3)) and day > today + timedelta(days=1):  # no year written: never in the future
        day = day.replace(year=day.year - 1)
    return day


class _Page:
    """The page's lines, and which of them have been read."""

    def __init__(self, text: str) -> None:
        lines = [" ".join(line.split()) for line in str(text or "").splitlines()]
        self.lines = [line for line in lines if line]
        self.used: set[int] = set()

    def labelled(self, i: int) -> int | None:
        """The amount on line ``i`` or, when it holds only a label, on the next line (which is then read)."""
        for j in (i, i + 1):
            found = _MONEY.search(self.lines[j]) if j < len(self.lines) else None
            if found:
                self.used.add(j)
                return parse_amount(found.group(1).replace(" ", ""))
        return None

    def plain(self, j: int) -> bool:
        """Words that could say who a transaction was with: not read yet, no amount, not a date, not a summary."""
        line = self.lines[j]
        return (
            j not in self.used and bool(_LETTERS.search(line)) and not _MONEY.search(line)
            and not _DATE.fullmatch(line.rstrip(":")) and not _SUMMARY.search(line) and not _ACCOUNT.search(line)
        )  # fmt: skip


def _labels(page: _Page, i: int, kind: str, out: PageMoney, today: date) -> bool:
    """Read line ``i`` as a labelled value, if it is one."""
    lines, line = page.lines, page.lines[i]
    if _SCORE_LABEL.search(line):
        window = " ".join(lines[i : i + 3])
        found = _SCORE.search(window)
        if found and 300 <= int(found.group(1)) <= 850:
            out.score = int(found.group(1))
            model = window.lower()
            out.score_model = "FICO" if "fico" in model else ("VantageScore" if "vantage" in model else "")
            page.used.update(j for j in (i + 1, i + 2) if j < len(lines) and found.group(1) in lines[j])
        return True
    if kind == "loan":
        if _LOAN_BALANCE.search(line):
            value = page.labelled(i)
            if out.loan_balance_cents is None:
                out.loan_balance_cents = value
            return True
        if _LOAN_PAYMENT.search(line):
            value = page.labelled(i)
            if out.loan_payment_cents is None:
                out.loan_payment_cents = value
            return True
        if _LOAN_DUE.search(line):
            found_day = page_date(" ".join(lines[i : i + 2]), today + timedelta(days=60))
            if found_day:
                out.loan_due = found_day
                page.used.add(i + 1)
            return True
    holding = _HOLDING.fullmatch(line)
    if holding:
        value = page.labelled(i)
        if value is not None:
            name = holding.group(1).lower()
            out.holdings["stocks" if name == "investing" else name] = abs(value)
        return True
    if _BALANCE.search(line) or _ACCOUNT.search(line):
        value = page.labelled(i)
        if value is not None and out.balance_cents is None and (_BALANCE.search(line) or kind in line.lower()):
            out.balance_cents = value  # the first tile of this account's kind ("Classic Checking"), or a balance
        return True
    return False


def read_page(text: str, today: date, kind: str = "", transactions: bool = True) -> PageMoney:
    """What ``text`` (a bank page's) says of the money. ``kind`` is the account's ("cash", "checking", "loan").
    ``transactions`` False: a summary page, whose amounts are balances and never transactions."""
    page = _Page(text)
    lines = page.lines
    out = PageMoney()
    heading: date | None = None
    for i, line in enumerate(lines):
        if i in page.used:
            continue
        amounts = _MONEY.findall(line)
        if not amounts and len(line) < 40 and (found_day := page_date(line, today)):
            if _DATE.fullmatch(line.rstrip(":")) or len(line.split()) <= 4:
                heading = found_day
                page.used.add(i)
                continue
        if _labels(page, i, kind, out, today):
            continue
        if not transactions or len(amounts) != 1 or _SUMMARY.search(line):
            continue
        words = _DATE.sub(" ", _MONEY.sub(" ", line)).strip(" -·•|,")
        above: list[int] = []
        if not _LETTERS.search(words):
            for j in range(i - 1, max(-1, i - 3), -1):
                if not page.plain(j):
                    break
                above.insert(0, j)
            words = " ".join(lines[j] for j in above)
        if not words:
            continue
        day = page_date(line, today)
        if day is None:
            day = next((d for j in above if (d := page_date(lines[j], today))), None)
        if day is None:
            day = heading
        if day is None and i + 1 < len(lines) and not _MONEY.search(lines[i + 1]):
            day = page_date(lines[i + 1], today)
        if day is None:
            continue
        raw = amounts[0].replace(" ", "")
        cents = parse_amount(raw)
        if not cents:
            continue
        if not any(c in raw for c in "+-−"):  # no sign written ("$8.40"): money out, unless its words say in
            cents = abs(cents) if _IN_WORDS.search(f"{line} {words}") else -abs(cents)
        page.used.update(above)
        out.rows.append(
            Row(day=day, cents=cents, description=mask(words)[:200], pending=bool(_PENDING.search(f"{line} {words}")))
        )
    return out
