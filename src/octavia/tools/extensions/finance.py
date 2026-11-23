"""Money: the user's accounts as plain facts, read-only, from her own record on this PC (``octavia/finance``).

``finance_look`` is the one way she reads it: an overview (balances, this month, recurring payments coming up, income,
whether the money lasts to payday, loans, the credit score, what's new), or one part of it, or the transactions
themselves. The rest feed it: ``finance_import`` (a statement file), ``finance_sign_in`` (the user signs in to a
bank in her bank browser), ``finance_refresh`` (read the bank pages now), ``finance_record`` (a balance, a credit
score or a loan's terms the user tells her), ``finance_controls`` (status, or delete all of it).

None of these can move money, pay, or sign in for the user. What she advises is hers to work out from the facts.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field

from octavia.finance import privacy
from octavia.finance import service as finance
from octavia.finance.analysis import Picture, day_words, facts, money
from octavia.finance.importers import ImportError_, parse_date
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

HEADER = "Money, from her own record on this PC (private: never read it out with others around)."
OFF = (
    "Money isn't on: it's turned off in her settings (Privacy, Money), or she isn't running in voice mode. "
    "Nothing about the user's money is kept."
)
EMPTY = (
    "There's nothing about the user's money yet. Two ways in: they drop a statement from USAA, Cash App or MOHELA "
    "(CSV, OFX or QFX) in {inbox} or name a file for finance_import; or they sign in once to a bank's page in her "
    "bank browser (finance_sign_in), which she then reads a few times a day."
)
Part = Literal["overview", "transactions", "recurring", "income", "spending", "changes", "loans", "credit", "accounts"]
Bank = Literal["cash_app", "usaa", "mohela"]


def _service() -> finance.FinanceService | None:
    return finance.SERVICE


def _date(value: str | None, today: date) -> date | None:
    if not value:
        return None
    word = value.strip().lower()
    if word == "today":
        return today
    if word == "yesterday":
        return today - timedelta(days=1)
    found = parse_date(value)
    if found is None:
        raise ValueError(f"{value!r} isn't a date; use YYYY-MM-DD")
    return found


class _FinanceTool(Tool):
    category = "finance"


# ----------------------------------------------------------------------------------------------------- reading


def _accounts(p: Picture) -> list[str]:
    lines = []
    for a in p.accounts:
        now = p.balance_now(a.id)
        if now is None:
            lines.append(f"- {a.label}: no balance read yet.")
            continue
        cents, day = now
        if a.kind == "loan":
            lines.append(f"- {a.label}: owes {money(cents)} (as of {day_words(day, p.today)}).")
        else:
            lines.append(f"- {a.label}: {money(cents, sign=True)} (balance read {day_words(day, p.today)}).")
    seen: set[tuple[int, str]] = set()
    for account_id, day, name, cents in p.holdings:
        if (account_id, name) in seen:
            continue
        seen.add((account_id, name))
        a = p.account(account_id)
        lines.append(f"- {a.label if a else 'An account'} {name}: {money(cents)} (as of {day_words(day, p.today)}).")
    return lines


def _month(p: Picture) -> list[str]:
    start = p.today.replace(day=1)
    spent = p.spending(start)
    came_in = [t for t in p.txns if t.cents > 0 and t.kind in ("income", "deposit", "refund", "interest")
               and t.day >= start]  # fmt: skip
    out = -sum(t.cents for t in spent)
    by: dict[str, int] = {}
    for t in spent:
        by[t.merchant] = by.get(t.merchant, 0) - t.cents
    top = ", ".join(f"{name} {money(c)}" for name, c in sorted(by.items(), key=lambda kv: -kv[1])[:5])
    lines = [f"This month so far: spent {money(out)}, came in {money(sum(t.cents for t in came_in))}."]
    if top:
        lines.append(f"Most spent at: {top}.")
    moved = [t for t in p.txns if t.kind == "transfer" and t.day >= start]
    if moved:
        lines.append(f"Moved between your own accounts this month (not counted as spending): {len(moved)} transfers.")
    return lines


def _recurring(p: Picture, income: bool) -> list[str]:
    found = p.income if income else p.payments
    lines = []
    for s in found:
        if not s.active:
            continue
        if s.variable:
            amount = f"about {money(s.typical_cents)}"
        elif s.last_cents != s.typical_cents:
            amount = f"{money(s.last_cents)} (was {money(s.typical_cents)})"
        else:
            amount = money(s.last_cents)
        nxt = (
            f"next around {day_words(s.next_day, p.today)}"
            if s.next_day >= p.today
            else f"was due {day_words(s.next_day, p.today)}"
        )
        lines.append(
            f"- {s.name}: {amount} {s.cadence}, last {day_words(s.last_day, p.today)}, {nxt} ({s.count} seen)."
        )
    if not lines:
        what = "income" if income else "recurring payments"
        lines.append(f"No regular {what} found yet (it takes about three of them).")
    return lines


def _runway(p: Picture) -> list[str]:
    spendable = p.spendable()
    if spendable is None:
        return ["No balance for checking or Cash App yet, so whether it lasts to payday can't be worked out."]
    payday = p.next_payday()
    end = payday or p.today + timedelta(days=14)
    due = p.due_before(end)
    pace = p.daily_pace()
    days = max(0, (end - p.today).days)
    left = spendable[0] - sum(s.typical_cents for s in due) - pace * days
    until = (
        f"the next paycheck ({day_words(payday, p.today)})"
        if payday
        else "two weeks from now (no regular income found)"
    )
    stale = (p.today - spendable[1]).days
    old = f" (the balance is {stale} days old)" if stale > 3 else ""
    return [
        f"Spendable now: {money(spendable[0], sign=True)}{old}. Until {until}: recurring payments "
        f"{money(sum(s.typical_cents for s in due))}, everyday spending about {money(pace)} a day, which leaves "
        f"about {money(left, sign=True)}."
    ]


def _loans(p: Picture) -> list[str]:
    lines = []
    for a in p.accounts:
        if a.kind != "loan":
            continue
        payment, due_day, rate = p.loans.get(a.id, (None, None, None))
        now = p.balance_now(a.id)
        parts = [f"owes {money(now[0])}" if now else "balance not read yet"]
        if payment:
            parts.append(f"payment {money(payment)}")
        if due_day:
            parts.append(
                f"due on the {due_day}{'th' if 10 < due_day % 100 < 14 else {1: 'st', 2: 'nd', 3: 'rd'}.get(due_day % 10, 'th')}"
            )
        if rate:
            parts.append(f"{rate:g}% interest")
        lines.append(f"- {a.label}: " + ", ".join(parts) + ".")
    paid = [t for t in p.txns if t.kind == "loan_payment"][:3]
    if paid:
        lines.append(
            "Recent loan payments: " + ", ".join(f"{money(t.cents)} {day_words(t.day, p.today)}" for t in paid) + "."
        )
    return lines or ["No loans known yet."]


def _credit(p: Picture) -> list[str]:
    if not p.scores:
        return ["No credit score yet: she reads it from the Cash App or USAA page, or the user tells her."]
    rows = [
        f"{s} ({m}) {day_words(d, p.today)}" if m else f"{s} {day_words(d, p.today)}" for d, s, m, _ in p.scores[:4]
    ]
    return ["Credit score, newest first: " + "; ".join(rows) + "."]


def _transactions(p: Picture, args: FinanceLookArgs) -> list[str]:
    since = _date(args.since, p.today) or p.today - timedelta(days=30)
    until = _date(args.until, p.today) or p.today
    want = (args.query or "").lower()
    names = {a.id: a.label for a in p.accounts}
    rows = [
        t for t in p.txns
        if since <= t.day <= until
        and (not want or want in f"{t.merchant} {t.description} {t.category} {t.memo}".lower())
        and (not args.account or args.account.lower() in names.get(t.account_id, "").lower())
    ]  # fmt: skip
    rows.sort(key=lambda t: (t.day, t.id), reverse=True)
    out_total = -sum(t.cents for t in rows if t.cents < 0 and t.kind not in ("transfer", "funding", "investment"))
    in_total = sum(t.cents for t in rows if t.cents > 0 and t.kind not in ("transfer",))
    lines = [f"{len(rows)} transactions from {day_words(since, p.today)} to {day_words(until, p.today)}: out "
             f"{money(out_total)} (not counting transfers), in {money(in_total)}."]  # fmt: skip
    for t in rows[: args.limit]:
        tags = [t.kind.replace("_", " ")] if t.kind not in ("purchase", "deposit") else []
        if t.pending:
            tags.append("pending")
        memo = f" — memo (their words): “{t.memo}”" if t.memo else ""
        tag = f" [{', '.join(tags)}]" if tags else ""
        lines.append(
            f"- {t.day:%b %d} {money(t.cents, sign=True)} {t.merchant}{tag} ({names.get(t.account_id, '')}){memo}"
        )
    if len(rows) > args.limit:
        lines.append(f"…and {len(rows) - args.limit} more (narrow it with query, since or until).")
    return lines


class FinanceLookArgs(ToolArgs):
    part: Part = Field(
        default="overview",
        description=(
            "overview: balances, this month, what's coming up, whether it lasts to payday, loans, credit, what's new. "
            "transactions: the transactions themselves (query/since/until/account narrow them). recurring: "
            "subscriptions and bills. income: paychecks. spending: this month by merchant. changes: what's new "
            "or worth knowing now. loans, credit, accounts: just that."
        ),
    )
    query: str | None = Field(default=None, max_length=80, description="transactions: words in the merchant or memo.")
    since: str | None = Field(
        default=None, description="transactions: from this day (YYYY-MM-DD). Default 30 days ago."
    )
    until: str | None = Field(default=None, description="transactions: to this day (YYYY-MM-DD). Default today.")
    account: str | None = Field(default=None, max_length=40, description="transactions: only this account ('usaa').")
    limit: int = Field(default=25, ge=1, le=100)


class FinanceLook(_FinanceTool):
    name = "finance_look"
    description = (
        "The user's money, from her own read-only record: balances, spending, transactions, recurring payments and "
        "subscriptions, income, whether money lasts to payday, student loans, credit score, and what's new or worth "
        "knowing (price rises, odd or double charges, fees, late paychecks). Use it to answer money questions and "
        "to tailor advice: the facts are given, what to suggest is yours. Never read amounts out with others "
        "around. She can't move money or pay anything."
    )
    Args = FinanceLookArgs
    risk = RiskLevel.SAFE

    async def run(self, args: FinanceLookArgs, ctx: ToolContext) -> ToolResult:
        service = _service()
        if service is None:
            return ToolResult.success(OFF)
        p = await service.picture()
        if not p.accounts:
            return ToolResult.success(EMPTY.format(inbox=service.options.inbox))
        try:
            lines = self._part(args, p, service)
        except ValueError as exc:
            return ToolResult.error(str(exc))
        newest = max((t.day for t in p.txns), default=None)
        fresh = f" Newest transaction: {day_words(newest, p.today)}." if newest else ""
        return ToolResult.success("\n".join([HEADER + fresh, *lines]), data={"part": args.part})

    def _part(self, args: FinanceLookArgs, p: Picture, service: finance.FinanceService) -> list[str]:
        if args.part == "transactions":
            return _transactions(p, args)
        if args.part == "recurring":
            return _recurring(p, income=False)
        if args.part == "income":
            return _recurring(p, income=True)
        if args.part == "spending":
            return _month(p)
        if args.part == "loans":
            return _loans(p)
        if args.part == "credit":
            return _credit(p)
        if args.part == "accounts":
            return _accounts(p)
        found = facts(p, service.options.low_balance_cents)
        news = [f"- {f.text}" for f in found] or ["- Nothing out of the ordinary."]
        if args.part == "changes":
            return ["Worth knowing now:", *news]
        upcoming = [f"- {s.name} {money(s.typical_cents)} {day_words(s.next_day, p.today)}"
                    for s in p.due_before(p.today + timedelta(days=14))]  # fmt: skip
        return [
            "Accounts:", *_accounts(p),
            *_month(p),
            *_runway(p),
            "Recurring payments in the next two weeks:", *(upcoming or ["- none found"]),
            "Income:", *_recurring(p, income=True),
            "Loans:", *_loans(p),
            *_credit(p),
            "Worth knowing now:", *news,
        ]  # fmt: skip


# ----------------------------------------------------------------------------------------------------- feeding it


class FinanceImportArgs(ToolArgs):
    path: str = Field(description="The statement file (CSV, OFX, QFX or QBO) the user downloaded from their bank.")


class FinanceImport(_FinanceTool):
    name = "finance_import"
    description = (
        "Import a bank statement file the user names (CSV, OFX, QFX or QBO from USAA, Cash App or MOHELA) into her "
        "money record. Files dropped in her money inbox are imported by themselves."
    )
    Args = FinanceImportArgs
    risk = RiskLevel.SAFE
    timeout_s = 60.0

    async def run(self, args: FinanceImportArgs, ctx: ToolContext) -> ToolResult:
        service = _service()
        if service is None:
            return ToolResult.success(OFF)
        path = Path(args.path.strip().strip('"')).expanduser()
        if not path.is_file():
            return ToolResult.error(f"There's no file at {path}.")
        try:
            _added, said = await service.import_file(path)
        except ImportError_ as exc:
            return ToolResult.error(f"{path.name} couldn't be imported: {exc}.")
        return ToolResult.success(said)


class FinanceSignInArgs(ToolArgs):
    bank: Bank = Field(description="Which bank's page to sign in to.")


class FinanceSignIn(_FinanceTool):
    name = "finance_sign_in"
    description = (
        "Open a bank's page (Cash App, USAA or MOHELA) in her own bank browser so the user signs in there "
        "themselves, once; after that she reads that page a few times a day, read-only. Never ask for a password "
        "or a code aloud."
    )
    Args = FinanceSignInArgs
    risk = RiskLevel.SAFE
    timeout_s = 60.0

    async def run(self, args: FinanceSignInArgs, ctx: ToolContext) -> ToolResult:
        service = _service()
        if service is None:
            return ToolResult.success(OFF)
        try:
            return ToolResult.success(await service.sign_in(args.bank))
        except Exception as exc:
            return ToolResult.error(f"The bank browser didn't open: {exc}")


class FinanceRefreshArgs(ToolArgs):
    bank: Bank | None = Field(default=None, description="Only this bank's page. Default: every one signed in to.")


class FinanceRefresh(_FinanceTool):
    name = "finance_refresh"
    description = (
        "Read the user's bank pages now (the ones they signed in to with finance_sign_in) and any statements in her "
        "money inbox. Closes the sign-in window if it's still open (after they said they're done)."
    )
    Args = FinanceRefreshArgs
    risk = RiskLevel.SAFE
    timeout_s = 180.0

    async def run(self, args: FinanceRefreshArgs, ctx: ToolContext) -> ToolResult:
        service = _service()
        if service is None:
            return ToolResult.success(OFF)
        imported = await service.check_inbox()
        await service.read_pages(args.bank, close_window=True)
        await service.notice()
        lines = []
        if imported:
            lines.append(f"Imported {imported} new transactions from the inbox.")
        for site in service.options.sites:
            state = service.sites_state().get(site.key, {})
            if args.bank and site.key != args.bank:
                continue
            if not state.get("connected"):
                lines.append(f"{site.label}: not signed in (finance_sign_in).")
            elif state.get("signed_in"):
                lines.append(f"{site.label}: read; found {state.get('found', 'nothing new')}.")
            else:
                lines.append(f"{site.label}: couldn't read it: {state.get('why') or 'unknown'}.")
        lines.append("finance_look has the result.")
        return ToolResult.success("\n".join(lines))


class FinanceRecordArgs(ToolArgs):
    what: Literal["balance", "credit_score", "loan"] = Field(description="What the user told her.")
    bank: Bank | None = Field(default=None, description="balance/loan: whose account.")
    account_kind: Literal["checking", "savings", "cash", "investing"] = Field(
        default="checking", description="balance: which kind of account."
    )
    amount: float | None = Field(
        default=None, ge=-10_000_000, le=10_000_000, description="balance or loan balance, dollars."
    )
    score: int | None = Field(default=None, ge=300, le=850, description="credit_score: the score.")
    score_model: str = Field(default="", max_length=30, description="credit_score: FICO, VantageScore, or ''.")
    payment: float | None = Field(default=None, ge=0, le=100_000, description="loan: the monthly payment, dollars.")
    due_day: int | None = Field(default=None, ge=1, le=31, description="loan: the day of the month it's due.")
    rate: float | None = Field(default=None, ge=0, le=50, description="loan: the interest rate, percent.")


class FinanceRecord(_FinanceTool):
    name = "finance_record"
    description = (
        "Keep a money fact the user tells her (or that she read on their bank page at their request): a balance, "
        "their credit score, or a student loan's balance, monthly payment and due day."
    )
    Args = FinanceRecordArgs
    risk = RiskLevel.SAFE
    private_args = frozenset({"amount", "score", "payment", "rate"})

    async def run(self, args: FinanceRecordArgs, ctx: ToolContext) -> ToolResult:
        service = _service()
        if service is None:
            return ToolResult.success(OFF)
        if args.what == "credit_score":
            if args.score is None:
                return ToolResult.error("A credit score needs the score.")
            await service.record_score(args.score, args.score_model)
            return ToolResult.success(f"Kept the credit score ({args.score}).")
        if args.bank is None:
            return ToolResult.error("Say whose account: cash_app, usaa or mohela.")
        if args.what == "balance":
            if args.amount is None:
                return ToolResult.error("A balance needs the amount.")
            await service.record_balance(args.bank, args.account_kind, round(args.amount * 100))
            return ToolResult.success("Kept the balance.")
        cents = lambda v: None if v is None else round(v * 100)  # noqa: E731
        await service.record_loan(args.bank, cents(args.amount), cents(args.payment), args.due_day, args.rate)
        return ToolResult.success("Kept the loan's details.")


class FinanceControlsArgs(ToolArgs):
    action: Literal["status", "delete_everything"] = Field(
        default="status",
        description="status: what she has and from where. delete_everything: delete all of the user's money data "
        "(the record, the inbox's statements, the bank browser's sign-ins). Only when the user asks for it.",
    )


class FinanceControls(_FinanceTool):
    name = "finance_controls"
    description = (
        "What she keeps of the user's money and where it comes from, or delete all of it. To stop it altogether, "
        "the user turns Money off in her settings (Privacy)."
    )
    Args = FinanceControlsArgs
    risk = RiskLevel.SAFE

    def assess_risk(self, args: FinanceControlsArgs) -> RiskLevel:
        return RiskLevel.CONFIRM if args.action == "delete_everything" else self.risk

    async def describe_call(self, args: FinanceControlsArgs, ctx: ToolContext) -> str:
        if args.action == "delete_everything":
            return "delete everything I keep about your money, and sign out of your banks in my bank browser"
        return "check what I keep about your money"

    async def run(self, args: FinanceControlsArgs, ctx: ToolContext) -> ToolResult:
        service = _service()
        if args.action == "delete_everything":
            try:
                gone = await service.purge() if service is not None else privacy.purge(ctx.config.root)
            except OSError as exc:
                return ToolResult.error(f"Not all of it could be deleted: {exc}. Quitting her and trying again would.")
            if not gone:
                return ToolResult.success("There was nothing about their money to delete.")
            inbox = ctx.config.resolve_path(ctx.config.finance.inbox)
            theirs = (
                ""
                if inbox.is_relative_to(ctx.config.resolve_path(privacy.DATA_DIR))
                else (f" Statement files in their own folder {inbox} were left alone, and won't be imported again.")
            )
            return ToolResult.success("Deleted all of it: the money record, the inbox and the bank sign-ins." + theirs)
        if service is None:
            return ToolResult.success(OFF)
        status = await service.status()
        counts = status["counts"]
        lines = [
            f"Kept on this PC: {counts['accounts']} accounts, {counts['transactions']} transactions, "
            f"{counts['balances']} balances, {counts['scores']} credit scores, {counts['imports']} statements imported.",
            f"Inbox for statements: {status['inbox']}.",
        ]
        for name, why in status.get("problems", {}).items():
            lines.append(f"Not imported from the inbox: {name}: {why}.")
        for site in service.options.sites:
            state = status["sites"].get(site.key, {})
            if not state.get("connected"):
                lines.append(f"{site.label}: not signed in.")
            else:
                ok = "reading" if state.get("signed_in") else f"not reading ({state.get('why') or 'not read yet'})"
                lines.append(f"{site.label}: signed in once, {ok}.")
        return ToolResult.success("\n".join(lines))


TOOLS = [FinanceLook, FinanceImport, FinanceSignIn, FinanceRefresh, FinanceRecord, FinanceControls]
