"""The money record turned into plain facts: transfers matched, recurring payments and income found, and what's
worth knowing now (``facts``). Pure functions over the store's rows: no model, no advice. Her brain reads the facts
and decides what to say and what to suggest.

**Transfers.** Money between the user's own accounts isn't spending. A USAA debit and a Cash App credit of the same
amount within 3 days are one transfer. Cash App is sometimes the middle step of a payment: a USAA card charge to
"Cash App" with a Cash App payment of the same amount within 3 days is the same payment, counted once, on the Cash
App side (the USAA one becomes ``funding``). A Cash App line on USAA with nothing to match it stays a transfer while
there's no Cash App activity for that day yet, and counts as spending once there is (the money left, and nothing on
the Cash App side says it came back).

**Recurring.** The same merchant's charges at a steady interval (weekly, every two weeks, monthly, quarterly,
yearly): at least 3 of them (2 for yearly), most intervals inside the cadence's band. Income is found the same way
from money coming in (paychecks, benefits), never from transfers or refunds.

**Facts** (each with a key, so the same one is told once): a new monthly-looking charge, a recurring charge's
price moving, a charge far above what that merchant (or the user) usually spends, the same charge twice in a day, a
bank fee, income later than its usual day, the money not lasting to the next paycheck at the usual pace, a loan
payment coming due, the credit score moving, and spending well ahead of the usual month.
"""

from __future__ import annotations

import calendar
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta

from octavia.finance import normalize
from octavia.finance.store import Account, Txn

MATCH_DAYS = 3
SPENDABLE = ("checking", "cash")

# cadence: (name, low, high) in days between payments
CADENCES = (
    ("weekly", 5, 9),
    ("every two weeks", 12, 17),
    ("monthly", 26, 35),
    ("quarterly", 84, 98),
    ("yearly", 350, 380),
)


def money(cents: int, sign: bool = False) -> str:
    """ "$1,234.56" ("-$12.00" with ``sign`` for money out)."""
    value = abs(cents) / 100
    text = f"${value:,.2f}"
    if text.endswith(".00") and value >= 100:
        text = text[:-3]
    return ("-" if sign and cents < 0 else "") + text


def day_words(day: date, today: date) -> str:
    delta = (day - today).days
    if delta == 0:
        return "today"
    if delta == -1:
        return "yesterday"
    if delta == 1:
        return "tomorrow"
    if 1 < delta < 7:
        return f"{day:%A}"
    return f"{day:%b} {day.day}" + (f", {day.year}" if day.year != today.year else "")


# ----------------------------------------------------------------------------------------------- transfers


def label(txns: Sequence[Txn], accounts: Sequence[Account]) -> list[Txn]:
    """The transactions with each one's kind as it counts: transfers between own accounts matched, Cash App
    payments paid from the USAA card counted once."""
    by_id = {a.id: a for a in accounts}
    inst = {a.id: a.institution for a in accounts}
    cash_ids = {a.id for a in accounts if a.institution == "cash_app"}
    cash_days = [t.day for t in txns if t.account_id in cash_ids]
    cash_span = (min(cash_days), max(cash_days)) if cash_days else None
    out = {t.id: t for t in txns}
    used: set[int] = set()
    ordered = sorted(txns, key=lambda t: (t.day, t.id))
    by_cents: dict[int, list[Txn]] = defaultdict(list)
    for t in ordered:
        by_cents[t.cents].append(t)

    def near(t: Txn, other: Txn) -> bool:
        return abs((t.day - other.day).days) <= MATCH_DAYS

    for t in ordered:
        if t.id in used or t.account_id not in by_id:
            continue
        cash_line = normalize.is_cash_app(t.description) and t.account_id not in cash_ids
        if t.kind != "transfer" and not cash_line:
            continue
        mate = next(
            (o for o in by_cents.get(-t.cents, ()) if o.id not in used and o.id != t.id
             and o.account_id != t.account_id and o.kind == "transfer" and near(t, o)),
            None,
        )  # fmt: skip
        if mate is not None:
            used |= {t.id, mate.id}
            out[t.id], out[mate.id] = replace(t, kind="transfer"), replace(mate, kind="transfer")
            continue
        if cash_line and t.cents < 0:
            paid = next(
                (o for o in by_cents.get(t.cents, ()) if o.id not in used and o.account_id in cash_ids
                 and near(t, o) and o.kind in normalize.SPENDING_KINDS),
                None,
            )  # fmt: skip
            if paid is not None:
                used |= {t.id, paid.id}
                out[t.id] = replace(t, kind="funding")
                continue
            if cash_span and cash_span[0] <= t.day <= cash_span[1]:
                out[t.id] = replace(t, kind="purchase")  # left for Cash App, and nothing there says where
                continue
        if inst.get(t.account_id) != "cash_app" and t.kind == "transfer":
            out[t.id] = replace(t, kind="transfer")
    return [out[t.id] for t in txns]


# ----------------------------------------------------------------------------------------------- recurring


@dataclass(frozen=True)
class Stream:
    """A recurring payment, or recurring income."""

    mkey: str
    name: str
    income: bool
    cadence: str
    every_days: float
    typical_cents: int  # its usual size (positive)
    last_cents: int
    last_day: date
    next_day: date
    count: int
    variable: bool  # the amount moves (a utility bill), so a new amount isn't a price change
    active: bool


def _cadence(intervals: list[int]) -> tuple[str, float] | None:
    if not intervals:
        return None
    med = statistics.median(intervals)
    for name, low, high in CADENCES:
        if low <= med <= high:
            inside = sum(1 for i in intervals if low - 2 <= i <= high + 2)
            if inside / len(intervals) >= 0.6:
                return name, float(med)
    return None


def streams(txns: Iterable[Txn], today: date, *, income: bool = False) -> list[Stream]:
    groups: dict[str, list[Txn]] = defaultdict(list)
    for t in txns:
        if income:
            if t.cents > 0 and t.kind in ("income", "deposit"):
                groups[t.mkey].append(t)
        elif t.cents < 0 and t.kind in normalize.SPENDING_KINDS:
            groups[t.mkey].append(t)
    found: list[Stream] = []
    for mkey, items in groups.items():
        items.sort(key=lambda t: (t.day, t.id))
        days = sorted({t.day for t in items})
        if len(days) < 2:
            continue
        intervals = [(b - a).days for a, b in zip(days, days[1:], strict=False)]
        shape = _cadence(intervals)
        if shape is None or (len(days) < 3 and shape[0] != "yearly"):
            continue
        cadence, every = shape
        recent = [abs(t.cents) for t in items[-6:]]
        typical = int(statistics.median(recent))
        variable = typical > 0 and (max(recent) - min(recent)) / typical > 0.25
        last = items[-1]
        next_day = last.day + timedelta(days=round(every))
        active = (today - last.day).days <= every * 1.6 + 3
        found.append(
            Stream(mkey, last.merchant, income, cadence, every, typical, abs(last.cents), last.day, next_day,
                   len(days), variable, active)
        )  # fmt: skip
    found.sort(key=lambda s: (not s.active, s.next_day))
    return found


# ----------------------------------------------------------------------------------------------- the picture


@dataclass
class Picture:
    today: date
    accounts: list[Account]
    txns: list[Txn]  # labelled
    balances: dict[int, tuple[date, int, str]]
    payments: list[Stream]
    income: list[Stream]
    scores: list[tuple[date, int, str, str]]
    loans: dict[int, tuple[int | None, int | None, float | None]]
    holdings: list[tuple[int, date, str, int]]

    def account(self, account_id: int) -> Account | None:
        return next((a for a in self.accounts if a.id == account_id), None)

    def balance_now(self, account_id: int) -> tuple[int, date] | None:
        """The latest balance, plus whatever came in or went out after the day it was read."""
        found = self.balances.get(account_id)
        if found is None:
            return None
        day, cents, _ = found
        later = sum(t.cents for t in self.txns if t.account_id == account_id and t.day > day and not t.pending)
        return cents + later, day

    def spendable(self) -> tuple[int, date] | None:
        """Checking and Cash App cash together, and the oldest day among them."""
        total, oldest, any_found = 0, self.today, False
        for a in self.accounts:
            if a.kind in SPENDABLE and (now := self.balance_now(a.id)) is not None:
                total += now[0]
                oldest = min(oldest, now[1])
                any_found = True
        return (total, oldest) if any_found else None

    def spending(self, since: date, until: date | None = None) -> list[Txn]:
        until = until or self.today
        return [t for t in self.txns if t.cents < 0 and t.kind in normalize.SPENDING_KINDS and since <= t.day <= until]

    def next_payday(self) -> date | None:
        days = [s.next_day for s in self.income if s.active and s.next_day >= self.today]
        return min(days) if days else None

    def daily_pace(self, days: int = 56) -> int:
        """What the user spends a day, in cents, besides their recurring payments (the last ``days`` days)."""
        recurring = {s.mkey for s in self.payments if s.active}
        since = self.today - timedelta(days=days)
        spent = [t for t in self.spending(since) if t.mkey not in recurring]
        span = max(1, min(days, (self.today - min((t.day for t in self.txns), default=self.today)).days))
        return round(-sum(t.cents for t in spent) / span)

    def due_before(self, end: date) -> list[Stream]:
        return [s for s in self.payments if s.active and self.today <= s.next_day < end]


def build(
    today: date,
    accounts: list[Account],
    txns: list[Txn],
    balances: dict[int, tuple[date, int, str]],
    scores: list[tuple[date, int, str, str]],
    loans: dict[int, tuple[int | None, int | None, float | None]],
    holdings: list[tuple[int, date, str, int]],
) -> Picture:
    labelled = label(txns, accounts)
    paid_into = {
        a.id for a in accounts if a.kind in ("checking", "savings", "cash")
    }  # income lands here, not on a loan
    return Picture(
        today, accounts, labelled, balances, streams(labelled, today),
        streams([t for t in labelled if t.account_id in paid_into], today, income=True),
        scores, loans, holdings,
    )  # fmt: skip


# ----------------------------------------------------------------------------------------------- facts


@dataclass(frozen=True)
class Fact:
    key: str  # the same fact keeps its key: it's told once
    kind: str
    text: str  # plain facts, never advice
    soon: bool = False  # it matters before the next few days are out
    due: date | None = None


def facts(p: Picture, low_balance_cents: int = 10_000) -> list[Fact]:
    out: list[Fact] = []
    today = p.today
    recent = today - timedelta(days=10)
    by_mkey: dict[str, list[Txn]] = defaultdict(list)
    for t in p.spending(today - timedelta(days=400)):
        by_mkey[t.mkey].append(t)
    stream_keys = {s.mkey for s in p.payments}

    # a new monthly-looking charge: twice, a month apart, the second lately, about the same amount
    for mkey, items in by_mkey.items():
        if mkey in stream_keys or len({t.day for t in items}) != 2:
            continue
        items = sorted(items, key=lambda t: t.day)
        a, b = items[0], items[-1]
        gap = (b.day - a.day).days
        if 26 <= gap <= 35 and b.day >= recent and abs(a.cents - b.cents) <= 0.1 * abs(a.cents):
            out.append(Fact(f"new:{mkey}:{b.day:%Y-%m}", "new_recurring",
                            f"A charge that looks new and monthly: {b.merchant}, {money(a.cents)} on "
                            f"{day_words(a.day, today)} and {money(b.cents)} on {day_words(b.day, today)}."))  # fmt: skip

    # a recurring charge's price moved
    for s in p.payments:
        if not s.active or s.variable or s.count < 3 or s.last_day < recent:
            continue
        before = [abs(t.cents) for t in sorted(by_mkey.get(s.mkey, []), key=lambda t: t.day)[:-1]][-5:]
        if not before:
            continue
        usual = int(statistics.median(before))
        if usual and abs(s.last_cents - usual) >= max(100, 0.05 * usual):
            way = "up" if s.last_cents > usual else "down"
            out.append(Fact(f"price:{s.mkey}:{s.last_day}", "price_change",
                            f"{s.name} ({s.cadence}) went {way}: {money(s.last_cents)} on "
                            f"{day_words(s.last_day, today)}, usually {money(usual)}."))  # fmt: skip

    # far above the usual for that merchant, or for the user
    everyday = [abs(t.cents) for t in p.spending(today - timedelta(days=90))]
    usual_any = statistics.median(everyday) if everyday else 0
    for t in p.spending(recent):
        if t.kind == "loan_payment":
            continue
        history = [abs(o.cents) for o in by_mkey.get(t.mkey, []) if o.day < t.day]
        size = abs(t.cents)
        if len(history) >= 3:
            usual = statistics.median(history)
            odd = size >= 2.5 * usual and size - usual >= 2500
            why = f"usually {money(int(usual))} there"
        else:
            odd = size >= max(15_000, 4 * usual_any)
            why = "a new place, and well above your usual charge" if not history else "well above your usual charge"
        if odd:
            out.append(Fact(f"odd:{t.id}", "odd_charge",
                            f"{money(t.cents)} at {t.merchant} on {day_words(t.day, today)}: {why}."))  # fmt: skip

    # the same charge twice in one day
    twice: dict[tuple[str, date, int], list[Txn]] = defaultdict(list)
    for t in p.spending(recent):
        if abs(t.cents) >= 1000:
            twice[(t.mkey, t.day, t.cents)].append(t)
    for (mkey, day, cents), items in twice.items():
        if len(items) >= 2:
            out.append(Fact(f"double:{mkey}:{day}:{cents}", "double_charge",
                            f"{items[0].merchant} charged {money(cents)} {len(items)} times on "
                            f"{day_words(day, today)}.", soon=True))  # fmt: skip

    # bank fees
    for t in p.spending(today - timedelta(days=14)):
        if t.kind == "fee":
            out.append(Fact(f"fee:{t.id}", "fee",
                            f"A fee: {money(t.cents)}, “{t.merchant}”, on {day_words(t.day, today)}."))  # fmt: skip

    # income later than its usual day
    for s in p.income:
        grace = 2 if s.cadence == "weekly" else 3
        if s.active and s.next_day + timedelta(days=grace) < today:
            out.append(Fact(f"late:{s.mkey}:{s.next_day}", "income_late",
                            f"{s.name} ({s.cadence}, usually {money(s.typical_cents)}) was due around "
                            f"{day_words(s.next_day, today)} and hasn't come in.", soon=True, due=s.next_day))  # fmt: skip

    # will the money last to the next paycheck?
    spendable = p.spendable()
    if spendable is not None and (today - spendable[1]).days <= 7:
        payday = p.next_payday() or today + timedelta(days=14)
        due = p.due_before(payday)
        due_total = sum(s.typical_cents for s in due)
        days = max(0, (payday - today).days)
        pace = p.daily_pace() * days
        left = spendable[0] - due_total - pace
        if left < low_balance_cents:
            bills = ", ".join(f"{s.name} {money(s.typical_cents)} {day_words(s.next_day, today)}" for s in due[:5])
            paid = "the next paycheck" if p.next_payday() else "two weeks from now (no regular income found)"
            out.append(Fact(f"runway:{payday}:{left < 0}", "low_runway",
                            f"Spendable money now {money(spendable[0], sign=True)} (checking and Cash App). Before {paid} "
                            f"({day_words(payday, today)}): recurring payments {money(due_total)}"
                            + (f" ({bills})" if bills else "")
                            + f", and about {money(pace)} more at the usual pace of {money(p.daily_pace())} a day. "
                            f"That leaves about {money(left, sign=True)}.", soon=True, due=payday))  # fmt: skip

    # loan payments coming due
    for account_id, (payment, due_day, _rate) in p.loans.items():
        if not due_day:
            continue
        due = _next_on_day(today, int(due_day))
        if (due - today).days > 5:
            continue
        paid = [t for t in p.txns if t.kind == "loan_payment" and (today - t.day).days <= 20]
        if not paid:
            account = p.account(account_id)
            what = account.label if account else "the student loan"
            amount = f" of {money(payment)}" if payment else ""
            out.append(Fact(f"loan:{account_id}:{due}", "loan_due",
                            f"{what} payment{amount} is due {day_words(due, today)}, and no payment to it shows in "
                            "the last 20 days.", soon=True, due=due))  # fmt: skip

    # the credit score moved
    if len(p.scores) >= 2:
        (day, score, model, _), (_, before, _, _) = p.scores[0], p.scores[1]
        if abs(score - before) >= 10:
            way = "up" if score > before else "down"
            out.append(Fact(f"score:{day}:{score}", "score_change",
                            f"Credit score{f' ({model})' if model else ''} went {way} to {score} from {before} "
                            f"({day_words(day, today)})."))  # fmt: skip

    # spending well ahead of the usual month
    month_start = today.replace(day=1)
    so_far = -sum(t.cents for t in p.spending(month_start))
    earlier = []
    for back in (1, 2, 3):
        start = _months_back(month_start, back)
        end = start + timedelta(days=(today - month_start).days)
        if not any(t.day < start for t in p.txns):
            break
        earlier.append(-sum(t.cents for t in p.spending(start, end)))
    if len(earlier) == 3:
        usual = statistics.mean(earlier)
        if usual > 0 and so_far >= 1.4 * usual and so_far - usual >= 10_000:
            out.append(Fact(f"pace:{today:%Y-%m}", "spending_up",
                            f"Spent {money(so_far)} so far this month, against about {money(int(usual))} by this "
                            "day in each of the last three months."))  # fmt: skip
    return out


def _next_on_day(today: date, day_of_month: int) -> date:
    """The next date on that day of the month (the month's last day when it's shorter: due the 31st is the 30th)."""
    this = today.replace(day=min(day_of_month, calendar.monthrange(today.year, today.month)[1]))
    if this >= today:
        return this
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return date(year, month, min(day_of_month, calendar.monthrange(year, month)[1]))


def _months_back(first: date, n: int) -> date:
    month, year = first.month - n, first.year
    while month <= 0:
        month, year = month + 12, year - 1
    return first.replace(year=year, month=month)
