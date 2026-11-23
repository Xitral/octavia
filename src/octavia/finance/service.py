"""Money in the background: statements dropped in the inbox are imported, the bank pages the user signed in to are
read a few times a day, and what's new among the facts goes to her heads-ups (``core/heads_up.py``), where one short
judgment decides whether it's worth saying now and her brain says it in her own words.

One ``FinanceService`` runs in voice mode (``App._start_finance``), set as ``SERVICE`` for her tools. Every store call
runs on a worker thread under one lock, and the store is only reached inside it (``_db``), so a purge, which holds
the lock from the first step to the last, can't have the database opened again under it. Nothing is read from a bank
the user hasn't signed in to through ``finance_sign_in``, nothing is read while the sign-in window is open, and the
window is closed after ``SHOWN_MAX_S`` (the browser's debugging port is open while it runs).

A statement in the inbox that can't be imported is kept out of the next looks (by its hash) and named in the status,
so a bad file neither stops the rest nor is read again every minute. After a purge, the statements still in an inbox
of the user's own (outside ``data/finance``) are remembered by hash in ``purged.json`` and never imported again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, TypeVar

from octavia.finance import analysis, extract, importers, privacy
from octavia.finance.analysis import Fact, Picture
from octavia.finance.reader import DEFAULT_SITES, BankReader, PageRead, Site
from octavia.finance.store import FinanceStore, file_sha

log = logging.getLogger(__name__)

T = TypeVar("T")

SERVICE: FinanceService | None = None
TICK_S = 60.0
FIRST_READ_S = 10 * 60.0  # the first page read after she starts
SHOWN_MAX_S = 10 * 60.0  # a sign-in window left open this long is closed, so reading can go on
MAX_NEWS = 5  # facts handed to the heads-ups at once
PURGED_FILE = "purged.json"


def set_service(service: FinanceService | None) -> None:
    global SERVICE
    SERVICE = service


class Purging(RuntimeError):
    """The money record is being deleted: nothing may open it now."""


@dataclass
class Options:
    inbox: Path
    reads_per_day: int = 3
    low_balance_cents: int = 10_000
    heads_ups: bool = True
    sites: Sequence[Site] = DEFAULT_SITES


@dataclass(frozen=True)
class News:
    """A fact for the heads-ups, in the shape ``core.heads_up.News`` takes (the app makes one of these from it).
    ``facts`` are plain: the heads-ups add the note that money is private (``privacy.MONEY_NOTE``)."""

    key: str
    facts: str
    subject: str
    due_ts: float | None
    soon: bool


def as_news(fact: Fact) -> News:
    due = time.mktime(fact.due.timetuple()) + 12 * 3600 if fact.due else None
    return News(f"finance:{fact.key}", fact.text, f"finance:{fact.kind}", due, fact.soon)


class FinanceService:
    def __init__(
        self,
        root: Path,
        options: Options,
        *,
        news: Callable[[list[News]], None] | None = None,
        reader: BankReader | None = None,
        today: Callable[[], date] = date.today,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self.options = options
        self.folder = self.root / privacy.DATA_DIR
        self.news = news
        self.reader = reader or BankReader(self.folder / "browser")
        self._today = today
        self._clock = clock
        self._store: FinanceStore | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._next_read = clock() + FIRST_READ_S
        self._shown_at: float | None = None
        self._closed = False
        self._purging = False
        self.problems: dict[str, str] = {}  # inbox file name -> why it wasn't imported
        self._rejected: set[str] = set()  # their hashes

    # ------------------------------------------------------------------ plumbing

    def _open(self) -> FinanceStore:
        if self._purging:
            raise Purging("the money record is being deleted")
        if self._store is None:
            self._store = FinanceStore(self.folder / "finance.db")
        return self._store

    async def _db(self, name: str, *args: Any) -> Any:
        """``FinanceStore.<name>(*args)``, on a worker thread under the lock."""
        async with self._lock:
            store = self._open()
            return await asyncio.to_thread(getattr(store, name), *args)

    async def _with(self, fn: Callable[[FinanceStore], T]) -> T:
        async with self._lock:
            store = self._open()
            return await asyncio.to_thread(fn, store)

    def _sites_file(self) -> Path:
        return self.folder / "sites.json"

    def sites_state(self) -> dict[str, dict[str, Any]]:
        try:
            found = json.loads(self._sites_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return found if isinstance(found, dict) else {}

    def _save_site(self, key: str, **values: Any) -> None:
        if self._purging:
            return
        state = self.sites_state()
        state.setdefault(key, {}).update(values)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._sites_file().write_text(json.dumps(state, indent=1), encoding="utf-8")

    def _purged(self) -> set[str]:
        try:
            found = json.loads((self.folder / PURGED_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        return {str(s) for s in found} if isinstance(found, list) else set()

    def site(self, key: str) -> Site | None:
        key = key.lower().replace(" ", "_")
        return next((s for s in self.options.sites if s.key == key or s.institution == key), None)

    def _tell(self, items: list[News]) -> bool:
        """Hand news to the heads-ups. False when there's nowhere to hand it (off, or not running)."""
        if not items or self.news is None or not self.options.heads_ups:
            return False
        self.news(items)
        return True

    async def start(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.options.inbox.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.get_running_loop().create_task(self._loop(), name="finance")

    async def close(self) -> None:
        self._closed = True
        await self._stop_loop()
        with contextlib.suppress(Exception):
            await self.reader.close()
        async with self._lock:
            if self._store is not None:
                await asyncio.to_thread(self._store.close)
                self._store = None

    async def _stop_loop(self) -> None:
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _loop(self) -> None:
        while not self._closed:
            changed = 0
            try:
                changed += await self.check_inbox()
            except (asyncio.CancelledError, Purging):
                raise
            except Exception:
                log.warning("The money inbox check failed", exc_info=True)
            try:
                if self.reader.showing and self._shown_at is not None and self._clock() - self._shown_at > SHOWN_MAX_S:
                    await self.reader.close()  # a sign-in window left open: closed, so its port isn't left open
                    self._shown_at = None
                if self._clock() >= self._next_read and self.options.reads_per_day > 0:
                    every = 86400.0 / self.options.reads_per_day
                    self._next_read = self._clock() + every * random.uniform(0.85, 1.15)
                    changed += await self.read_pages()
            except (asyncio.CancelledError, Purging):
                raise
            except Exception:
                log.warning("Reading the bank pages failed", exc_info=True)
            try:
                if changed:
                    await self.notice()
            except (asyncio.CancelledError, Purging):
                raise
            except Exception:
                log.warning("Noticing what changed in the money record failed", exc_info=True)
            await asyncio.sleep(TICK_S)

    # ------------------------------------------------------------------ statements

    async def check_inbox(self) -> int:
        """Import the statements in the inbox not imported before. Returns how many transactions were new. A file
        that can't be imported is named in ``problems`` and skipped from then on (until it changes)."""
        added = 0
        purged = self._purged()
        for path in importers.statement_files(self.options.inbox):
            try:
                sha = await asyncio.to_thread(_file_hash, path)
            except OSError:
                continue
            if sha in self._rejected or sha in purged:
                continue
            try:
                added += (await self.import_file(path, quiet=True))[0]
            except Purging:
                raise
            except Exception as exc:  # one bad file never stops the rest
                why = (
                    str(exc)
                    if isinstance(exc, importers.ImportError_)
                    else f"it couldn't be read ({type(exc).__name__})"
                )
                self._rejected.add(sha)
                self.problems[path.name] = why
                log.info("A file in the money inbox wasn't imported: %s", why)
        return added

    async def import_file(self, path: Path, quiet: bool = False) -> tuple[int, str]:
        """(new transactions, a sentence saying what happened). Raises ``ImportError_``."""
        path = Path(path)
        try:
            if path.stat().st_size > importers.MAX_BYTES:
                raise importers.ImportError_("it's too big for a statement")
            sha = await asyncio.to_thread(_file_hash, path)
        except OSError as exc:
            raise importers.ImportError_(f"it couldn't be opened ({exc.strerror or exc})") from exc
        if await self._db("imported", sha):
            return 0, f"{path.name} was imported before."
        try:
            statements = await asyncio.to_thread(importers.read_statements, path)
        except importers.ImportError_:
            raise
        except Exception as exc:
            raise importers.ImportError_(f"it couldn't be read ({type(exc).__name__}: {exc})") from exc
        if not all(s.institution for s in statements):
            raise importers.ImportError_(
                "it doesn't say whose statement it is; put usaa, cashapp or mohela in the file name and try again"
            )
        added = kept = rows = 0
        days = []
        for statement in statements:
            account = await self._db("account", statement.institution, statement.kind, statement.last4)
            new, old = await self._db("add_rows", account, statement.rows, statement.source)
            added, kept, rows = added + new, kept + old, rows + len(statement.rows)
            days += [r.day for r in statement.rows]
            if statement.balance_cents is not None and statement.balance_day is not None:
                await self._db("set_balance", account, statement.balance_day, statement.balance_cents, statement.source)
        await self._db("record_import", sha, path.name, rows)
        self.problems.pop(path.name, None)
        if not quiet and added:
            await self.notice()
        days.sort()
        span = f" from {days[0]:%b %d} to {days[-1]:%b %d, %Y}" if days else ""
        return added, f"Imported {path.name}: {added} new transactions{span} ({kept} were already there)."

    # ------------------------------------------------------------------ bank pages

    async def sign_in(self, key: str) -> str:
        site = self.site(key)
        if site is None:
            names = ", ".join(s.label for s in self.options.sites)
            return f"There's no bank page for {key!r}; the ones she can read are {names}."
        await self.reader.sign_in(site)
        self._shown_at = self._clock()
        self._save_site(site.key, connected=True, signed_in=None, why="signing in", at=self._clock())
        return (
            f"Opened {site.label}'s sign-in page in her own bank browser. The user signs in there themselves; when "
            "they say they're done, call finance_refresh to read it (that closes the window). The window closes by "
            f"itself after {round(SHOWN_MAX_S / 60)} minutes."
        )

    async def read_pages(self, only: str | None = None, *, close_window: bool = False) -> int:
        """Read every connected bank page (or ``only`` one). Returns how many changes were taken in."""
        if self.reader.showing:
            if not close_window:
                return 0
            await self.reader.close()
            self._shown_at = None
        state = self.sites_state()
        added = 0
        for site in self.options.sites:
            if (only and site.key != only) or not state.get(site.key, {}).get("connected"):
                continue
            try:
                page = await self.reader.read(site)
            except Exception as exc:
                log.info("Couldn't read %s: %s", site.label, exc)
                self._save_site(site.key, why=f"couldn't read it ({type(exc).__name__})", at=self._clock())
                continue
            added += await self._take_page(site, page)
        return added

    async def _take_page(self, site: Site, page: PageRead) -> int:
        before = self.sites_state().get(site.key, {})
        if not page.signed_in:
            self._save_site(site.key, signed_in=False, why=page.why, at=self._clock())
            if before.get("signed_in") is not False and page.why == "it wants a fresh sign-in":
                self._tell([News(f"finance:signout:{site.key}:{self._today()}",
                                 f"{site.label}'s sign-in in her bank browser ended, so she can't read it until the "
                                 "user signs in there again.", "finance:sign_in", None, False)])  # fmt: skip
            return 0
        money = extract.read_page(page.text, self._today(), site.kind, site.transactions)
        today, source = self._today(), f"page:{site.key}"

        def keep(store: FinanceStore) -> int:
            account = store.account(site.institution, site.kind, "")
            added = store.add_rows(account, money.rows, source)[0] if money.rows else 0
            if money.balance_cents is not None:
                store.set_balance(account, today, money.balance_cents, source)
            for name, cents in money.holdings.items():
                store.set_holding(account, today, name, cents, source)
            if money.score is not None:
                store.add_score(today, money.score, money.score_model or site.label, source)
            if site.kind == "loan":
                if money.loan_balance_cents is not None:
                    store.set_balance(account, today, -abs(money.loan_balance_cents), source)
                if money.loan_payment_cents is not None or money.loan_due is not None:
                    store.set_loan(account, money.loan_payment_cents, money.loan_due.day if money.loan_due else None)
            return added

        added = await self._with(keep)
        found = "nothing it could read as money" if money.empty else f"{len(money.rows)} transactions"
        self._save_site(site.key, signed_in=True, why="", at=self._clock(), last_read=self._clock(), found=found)
        return added + (0 if money.empty else 1)

    # ------------------------------------------------------------------ what she says

    async def picture(self) -> Picture:
        today = self._today()

        def build(store: FinanceStore) -> Picture:
            since = today - timedelta(days=400)
            return analysis.build(
                today, store.accounts(), store.transactions(since=since), store.latest_balances(), store.scores(),
                store.loans(), store.holdings(),
            )  # fmt: skip

        return await self._with(build)

    async def facts(self) -> list[Fact]:
        return analysis.facts(await self.picture(), self.options.low_balance_cents)

    async def notice(self) -> list[Fact]:
        """The facts not told before, handed to the heads-ups (the soonest first). Only what was handed over is
        marked as told: the rest waits for the next look."""
        found = await self.facts()
        fresh_keys = set(await self._db("unseen", [f.key for f in found]))
        fresh = sorted((f for f in found if f.key in fresh_keys), key=lambda f: (not f.soon, f.due or date.max))
        handed = fresh[:MAX_NEWS]
        if self._tell([as_news(f) for f in handed]):
            await self._db("mark_seen", [f.key for f in handed])
        return fresh

    # ------------------------------------------------------------------ the user's own words

    async def record_balance(self, institution: str, kind: str, cents: int, day: date | None = None) -> None:
        account = await self._db("account", institution, kind, "")
        await self._db("set_balance", account, day or self._today(), cents, "told")

    async def record_score(self, score: int, model: str = "", day: date | None = None) -> None:
        await self._db("add_score", day or self._today(), score, model, "told")
        await self.notice()

    async def record_loan(
        self, institution: str, balance_cents: int | None, payment_cents: int | None, due_day: int | None,
        rate: float | None,
    ) -> None:  # fmt: skip
        account = await self._db("account", institution, "loan", "")
        if balance_cents is not None:
            await self._db("set_balance", account, self._today(), -abs(balance_cents), "told")
        await self._db("set_loan", account, payment_cents, due_day, rate)
        await self.notice()

    async def status(self) -> dict[str, Any]:
        counts = await self._db("counts")
        newest = await self._db("newest_day")
        return {
            "counts": counts, "newest": newest, "sites": self.sites_state(), "inbox": str(self.options.inbox),
            "problems": dict(self.problems),
        }  # fmt: skip

    async def purge(self) -> list[str]:
        """Everything deleted: the record, the default inbox, the bank browser's sign-ins. Holds the lock from start to
        end, with the background loop stopped, so nothing opens the record again under it. Statements in an inbox of
        the user's own are left where they are and never imported again. Raises OSError when something's in use."""
        await self._stop_loop()
        async with self._lock:
            self._purging = True
            try:
                with contextlib.suppress(Exception):
                    await self.reader.close()
                self._shown_at = None
                if self._store is not None:
                    await asyncio.to_thread(self._store.close)
                    self._store = None
                theirs = []
                inbox = self.options.inbox
                if not _inside(inbox, self.folder):
                    for path in importers.statement_files(inbox):
                        with contextlib.suppress(OSError):
                            theirs.append(await asyncio.to_thread(_file_hash, path))
                gone = await asyncio.to_thread(privacy.purge, self.root)
                if theirs:
                    self.folder.mkdir(parents=True, exist_ok=True)
                    (self.folder / PURGED_FILE).write_text(json.dumps(sorted(theirs)), encoding="utf-8")
                self.problems.clear()
                self._rejected.clear()
            finally:
                self._purging = False
        if not self._closed:
            self._task = asyncio.get_running_loop().create_task(self._loop(), name="finance")
        return gone


def _file_hash(path: Path) -> str:
    return file_sha(Path(path).read_bytes())


def _inside(path: Path, folder: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(Path(folder).resolve())
    except OSError:
        return False
