"""The money record, in SQLite on this PC (``data/finance/finance.db``): accounts, transactions, balances, holdings,
credit scores, loan terms, the statements already imported, and the facts already told.

The same transaction often arrives twice: an export that overlaps the last one, or the bank page read after the
statement was imported (worded differently), or pending first and posted later. ``add_rows`` keeps one: the same
bank id, or the same day, amount and description (counted, so two identical coffees on one day stay two), or, from
another source, the same amount within 3 days not yet matched by anything else in that batch.

One connection, used from one thread at a time (``FinanceService`` runs every call through ``asyncio.to_thread``
under its lock). Migrations are numbered (``MIGRATIONS``, ``PRAGMA user_version``).
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from octavia.finance import normalize
from octavia.finance.importers import Row

MATCH_DAYS = 3

MIGRATIONS = [
    """
    CREATE TABLE accounts (
        id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, institution TEXT NOT NULL, kind TEXT NOT NULL,
        last4 TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '', created REAL NOT NULL
    );
    CREATE TABLE transactions (
        id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), day TEXT NOT NULL,
        cents INTEGER NOT NULL, description TEXT NOT NULL, merchant TEXT NOT NULL, mkey TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT '', memo TEXT NOT NULL DEFAULT '', hint TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL, pending INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL, bank_id TEXT NOT NULL DEFAULT '',
        fingerprint TEXT UNIQUE NOT NULL, added REAL NOT NULL
    );
    CREATE INDEX transactions_day ON transactions(day);
    CREATE INDEX transactions_mkey ON transactions(mkey);
    CREATE TABLE balances (
        account_id INTEGER NOT NULL REFERENCES accounts(id), day TEXT NOT NULL, cents INTEGER NOT NULL,
        source TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (account_id, day)
    );
    CREATE TABLE holdings (
        account_id INTEGER NOT NULL REFERENCES accounts(id), day TEXT NOT NULL, name TEXT NOT NULL,
        cents INTEGER NOT NULL, source TEXT NOT NULL, PRIMARY KEY (account_id, day, name)
    );
    CREATE TABLE scores (
        day TEXT NOT NULL, model TEXT NOT NULL, score INTEGER NOT NULL, source TEXT NOT NULL, at REAL NOT NULL,
        PRIMARY KEY (day, model)
    );
    CREATE TABLE loans (
        account_id INTEGER PRIMARY KEY REFERENCES accounts(id), payment_cents INTEGER, due_day INTEGER,
        rate REAL, updated REAL NOT NULL
    );
    CREATE TABLE imports (sha TEXT PRIMARY KEY, name TEXT NOT NULL, rows INTEGER NOT NULL, at REAL NOT NULL);
    CREATE TABLE facts_seen (key TEXT PRIMARY KEY, first REAL NOT NULL, last REAL NOT NULL);
    """,
]

NAMES = {"usaa": "USAA", "cash_app": "Cash App", "mohela": "MOHELA"}


@dataclass(frozen=True)
class Account:
    id: int
    institution: str
    kind: str
    last4: str
    name: str

    @property
    def label(self) -> str:
        """ "USAA checking …1234"."""
        if self.name:
            return self.name
        bank = NAMES.get(self.institution, self.institution.replace("_", " ").title() or "An account")
        kind = "" if self.kind in ("", "cash") else f" {self.kind}"
        return f"{bank}{kind}" + (f" …{self.last4}" if self.last4 else "")


@dataclass(frozen=True)
class Txn:
    id: int
    account_id: int
    day: date
    cents: int
    description: str
    merchant: str
    mkey: str
    kind: str
    category: str = ""
    memo: str = ""
    pending: bool = False
    source: str = ""


def file_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FinanceStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _migrate(self) -> None:
        with self._lock:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            for n, script in enumerate(MIGRATIONS[version:], start=version + 1):
                self._db.executescript(f"BEGIN; {script}; PRAGMA user_version = {n}; COMMIT;")

    # ------------------------------------------------------------------ accounts

    def account(self, institution: str, kind: str, last4: str = "", name: str = "") -> int:
        """The account's id, made if it's new."""
        key = f"{institution or 'other'}:{kind or 'account'}:{last4}"
        with self._lock:
            found = self._db.execute("SELECT id FROM accounts WHERE key = ?", (key,)).fetchone()
            if found:
                return int(found[0])
            if not last4:  # a statement without the number fits the account of that kind that has one
                found = self._db.execute(
                    "SELECT id FROM accounts WHERE institution = ? AND kind = ? ORDER BY id LIMIT 1",
                    (institution or "other", kind or "account"),
                ).fetchone()
                if found:
                    return int(found[0])
            else:  # the number, for an account kept so far without it (a page read, a CSV): it's that one
                found = self._db.execute(
                    "SELECT id FROM accounts WHERE institution = ? AND kind = ? AND last4 = '' ORDER BY id LIMIT 1",
                    (institution or "other", kind or "account"),
                ).fetchone()
                if found:
                    self._db.execute("UPDATE accounts SET last4 = ?, key = ? WHERE id = ?", (last4, key, found[0]))
                    self._db.commit()
                    return int(found[0])
            cur = self._db.execute(
                "INSERT INTO accounts (key, institution, kind, last4, name, created) VALUES (?, ?, ?, ?, ?, ?)",
                (key, institution or "other", kind or "account", last4, name, time.time()),
            )
            self._db.commit()
            return int(cur.lastrowid or 0)

    def accounts(self) -> list[Account]:
        with self._lock:
            rows = self._db.execute("SELECT id, institution, kind, last4, name FROM accounts ORDER BY id").fetchall()
        return [Account(int(i), str(a), str(k), str(l4), str(n)) for i, a, k, l4, n in rows]

    # ------------------------------------------------------------------ transactions

    def add_rows(self, account_id: int, rows: Iterable[Row], source: str) -> tuple[int, int]:
        """Keep the new ones: (added, already there)."""
        added = skipped = 0
        seen: Counter[tuple[str, int, str]] = Counter()
        claimed: set[int] = set()
        with self._lock:
            for row in rows:
                mkey = normalize.merchant_key(row.description)
                same = (row.day.isoformat(), row.cents, mkey)
                seen[same] += 1
                if row.bank_id:
                    fingerprint = f"{account_id}|id|{row.bank_id}"
                else:
                    fingerprint = f"{account_id}|{same[0]}|{same[1]}|{same[2]}|{seen[same]}"
                kept = self._db.execute("SELECT id FROM transactions WHERE fingerprint = ?", (fingerprint,)).fetchone()
                if kept:
                    claimed.add(int(kept[0]))  # so a second, similar row in this batch isn't matched to it
                    skipped += 1
                    if not row.pending:  # the pending one has posted
                        self._db.execute("UPDATE transactions SET pending = 0 WHERE id = ?", (int(kept[0]),))
                    continue
                twin = self._twin(account_id, row, source, claimed)
                if twin is not None:
                    claimed.add(twin)
                    skipped += 1
                    if not row.pending:
                        self._db.execute("UPDATE transactions SET pending = 0 WHERE id = ?", (twin,))
                    continue
                cur = self._db.execute(
                    "INSERT INTO transactions (account_id, day, cents, description, merchant, mkey, category, memo,"
                    " hint, kind, pending, source, bank_id, fingerprint, added)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        account_id,
                        same[0],
                        row.cents,
                        row.description,
                        normalize.merchant(row.description),
                        mkey,
                        row.category,
                        row.memo,
                        row.hint,
                        normalize.kind_of(row.description, row.cents, category=row.category, hint=row.hint),
                        int(row.pending),
                        source,
                        row.bank_id,
                        fingerprint,
                        time.time(),
                    ),
                )
                claimed.add(int(cur.lastrowid or 0))
                added += 1
            self._db.commit()
        return added, skipped

    def _twin(self, account_id: int, row: Row, source: str, claimed: set[int]) -> int | None:
        """The same transaction already kept, worded or dated a little differently: the same amount within a few
        days, at a merchant whose name starts the same (Starbucks, "STARBUCKS STORE 12"). Never two rows the bank
        gave different ids, and never one already matched in this batch."""
        lo, hi = (row.day - timedelta(days=MATCH_DAYS)).isoformat(), (row.day + timedelta(days=MATCH_DAYS)).isoformat()
        first = (normalize.merchant_key(row.description).split() or [""])[0]
        for txn_id, mkey, bank_id in self._db.execute(
            "SELECT id, mkey, bank_id FROM transactions WHERE account_id = ? AND cents = ? AND day BETWEEN ? AND ?"
            " ORDER BY ABS(julianday(day) - julianday(?)), id",
            (account_id, row.cents, lo, hi, row.day.isoformat()),
        ).fetchall():
            if int(txn_id) in claimed:
                continue
            if row.bank_id and bank_id and row.bank_id != bank_id:
                continue  # the bank says they're two
            if first and (str(mkey).split() or [""])[0] == first:
                return int(txn_id)
        return None

    def transactions(
        self,
        since: date | None = None,
        until: date | None = None,
        account_id: int | None = None,
        text: str | None = None,
        limit: int | None = None,
    ) -> list[Txn]:
        where, args = ["1"], []
        if since:
            where.append("day >= ?")
            args.append(since.isoformat())
        if until:
            where.append("day <= ?")
            args.append(until.isoformat())
        if account_id is not None:
            where.append("account_id = ?")
            args.append(account_id)
        if text:
            where.append("(description LIKE ? OR merchant LIKE ? OR memo LIKE ? OR category LIKE ?)")
            args += [f"%{text}%"] * 4
        sql = (
            "SELECT id, account_id, day, cents, description, merchant, mkey, kind, category, memo, pending, source"
            f" FROM transactions WHERE {' AND '.join(where)} ORDER BY day DESC, id DESC"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [
            Txn(int(i), int(a), date.fromisoformat(d), int(c), str(desc), str(m), str(k), str(kind), str(cat),
                str(memo), bool(p), str(src))
            for i, a, d, c, desc, m, k, kind, cat, memo, p, src in rows
        ]  # fmt: skip

    # ------------------------------------------------------------------ balances, holdings, scores, loans

    def set_balance(self, account_id: int, day: date, cents: int, source: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO balances (account_id, day, cents, source, at) VALUES (?, ?, ?, ?, ?)",
                (account_id, day.isoformat(), cents, source, time.time()),
            )
            self._db.commit()

    def balances(self, account_id: int | None = None) -> list[tuple[int, date, int, str]]:
        """(account, day, cents, source), newest first."""
        sql = "SELECT account_id, day, cents, source FROM balances"
        args: tuple[int, ...] = ()
        if account_id is not None:
            sql, args = sql + " WHERE account_id = ?", (account_id,)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY day DESC, at DESC", args).fetchall()
        return [(int(a), date.fromisoformat(d), int(c), str(s)) for a, d, c, s in rows]

    def latest_balances(self) -> dict[int, tuple[date, int, str]]:
        out: dict[int, tuple[date, int, str]] = {}
        for account_id, day, cents, source in self.balances():
            out.setdefault(account_id, (day, cents, source))
        return out

    def set_holding(self, account_id: int, day: date, name: str, cents: int, source: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO holdings (account_id, day, name, cents, source) VALUES (?, ?, ?, ?, ?)",
                (account_id, day.isoformat(), name[:60], cents, source),
            )
            self._db.commit()

    def holdings(self) -> list[tuple[int, date, str, int]]:
        """(account, day, name, cents), newest day first."""
        with self._lock:
            rows = self._db.execute("SELECT account_id, day, name, cents FROM holdings ORDER BY day DESC").fetchall()
        return [(int(a), date.fromisoformat(d), str(n), int(c)) for a, d, n, c in rows]

    def add_score(self, day: date, score: int, model: str, source: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO scores (day, model, score, source, at) VALUES (?, ?, ?, ?, ?)",
                (day.isoformat(), model[:40], score, source, time.time()),
            )
            self._db.commit()

    def scores(self) -> list[tuple[date, int, str, str]]:
        """(day, score, model, source), newest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT day, score, model, source FROM scores ORDER BY day DESC, at DESC"
            ).fetchall()
        return [(date.fromisoformat(d), int(s), str(m), str(src)) for d, s, m, src in rows]

    def set_loan(
        self, account_id: int, payment_cents: int | None = None, due_day: int | None = None, rate: float | None = None
    ) -> None:
        with self._lock:
            old = self._db.execute(
                "SELECT payment_cents, due_day, rate FROM loans WHERE account_id = ?", (account_id,)
            ).fetchone() or (None, None, None)
            self._db.execute(
                "INSERT OR REPLACE INTO loans (account_id, payment_cents, due_day, rate, updated) VALUES (?, ?, ?, ?, ?)",
                (
                    account_id,
                    payment_cents if payment_cents is not None else old[0],
                    due_day if due_day is not None else old[1],
                    rate if rate is not None else old[2],
                    time.time(),
                ),
            )
            self._db.commit()

    def loans(self) -> dict[int, tuple[int | None, int | None, float | None]]:
        with self._lock:
            rows = self._db.execute("SELECT account_id, payment_cents, due_day, rate FROM loans").fetchall()
        return {int(a): (p, d, r) for a, p, d, r in rows}

    # ------------------------------------------------------------------ imports and facts

    def imported(self, sha: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM imports WHERE sha = ?", (sha,)).fetchone() is not None

    def record_import(self, sha: str, name: str, rows: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO imports (sha, name, rows, at) VALUES (?, ?, ?, ?)",
                (sha, name, rows, time.time()),
            )
            self._db.commit()

    def unseen(self, keys: Iterable[str]) -> list[str]:
        """Which of these facts haven't been told before."""
        with self._lock:
            return [k for k in keys if not self._db.execute("SELECT 1 FROM facts_seen WHERE key = ?", (k,)).fetchone()]

    def mark_seen(self, keys: Iterable[str]) -> None:
        """These facts have been handed over: they aren't told again."""
        now = time.time()
        with self._lock:
            for key in keys:
                self._db.execute(
                    "INSERT INTO facts_seen (key, first, last) VALUES (?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET last = excluded.last",
                    (key, now, now),
                )
            self._db.commit()

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                table: int(self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("accounts", "transactions", "balances", "scores", "imports")
            }

    def newest_day(self) -> date | None:
        with self._lock:
            found = self._db.execute("SELECT MAX(day) FROM transactions").fetchone()[0]
        return date.fromisoformat(found) if found else None
