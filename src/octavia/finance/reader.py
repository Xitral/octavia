"""The bank browser: the user's bank pages, read in a browser profile of their own (``data/finance/browser``).

It is the same browser her web tasks use (``_web_browser.WebBrowser``), started on another profile, so a web task,
which can click and type, never runs where the user is signed in to a bank. This one only opens a page on
``privacy.READ_HOSTS`` and reads its text: this module calls nothing on a page that acts on it (a test holds it to
``privacy.FORBIDDEN_PAGE_CALLS``). A page that ends up anywhere else (a redirect) isn't read.

The user signs in once, in a window it shows (``sign_in``); the browser keeps the session in its profile, never the
password. A read that lands on a sign-in page instead reports ``signed_in=False``, so she can say the bank wants a
fresh sign-in. While the sign-in window is open, nothing is read (that would close it).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from octavia.finance.privacy import may_read

log = logging.getLogger(__name__)

OWNER = -4040  # the tab's owner id in the browser (a web task's are positive)
LATE_TRIES = 4
LATE_WAIT_S = 2.0
_SIGN_IN_PATH = re.compile(r"(?i)(?:^|/)(?:log[-_]?in|log[-_]?on|sign[-_]?in|auth|login\.html|account/login)")
_SIGN_IN_WORDS = re.compile(r"(?i)\b(?:password|passcode|sign[- ]in code|verification code|log ?in|sign ?in)\b")


@dataclass(frozen=True)
class Site:
    key: str  # "cash_app", "usaa", "mohela"
    institution: str
    kind: str  # the account kind its page shows ("cash", "checking", "loan")
    url: str
    label: str
    transactions: bool = True  # False: a summary page, read for balances only (its amounts aren't transactions)


DEFAULT_SITES: tuple[Site, ...] = (
    Site("cash_app", "cash_app", "cash", "https://cash.app/account/activity", "Cash App"),
    Site("usaa", "usaa", "checking", "https://www.usaa.com/my/accounts", "USAA", transactions=False),
    Site("mohela", "mohela", "loan", "https://mohela.studentaid.gov/", "MOHELA", transactions=False),
)


@dataclass(frozen=True)
class PageRead:
    site: str
    signed_in: bool
    url: str
    text: str
    why: str = ""


def _signed_out(url: str, text: str, items: list[dict[str, Any]]) -> bool:
    path = urlsplit(url).path or "/"
    if _SIGN_IN_PATH.search(path):
        return True
    asks = [i for i in items if i.get("role") == "textbox" and _SIGN_IN_WORDS.search(str(i.get("name", "")))]
    has_money = "$" in text
    return bool(asks) and not has_money


class BankReader:
    def __init__(
        self,
        profile: Path,
        browser: Any = None,
        make: Callable[[Path], Any] | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.profile = Path(profile)
        self._browser = browser
        self._make = make
        self._sleep = sleep

    @property
    def browser(self) -> Any:
        if self._browser is None:
            if self._make is not None:
                self._browser = self._make(self.profile)
            else:
                from octavia.tools.extensions._web_browser import WebBrowser

                self._browser = WebBrowser(self.profile)
        return self._browser

    @property
    def showing(self) -> bool:
        """Is the sign-in window open?"""
        b = self._browser
        return b is not None and bool(getattr(b, "running", False)) and not getattr(b, "headless", True)

    async def sign_in(self, site: Site) -> None:
        """The bank's page in a window the user sees, to sign in themselves."""
        if not may_read(site.url):
            raise ValueError(f"{site.url} isn't a page the bank browser may open")
        await self.browser.show(site.url)

    async def read(self, site: Site) -> PageRead:
        """The text of ``site``'s page, read out of sight."""
        if not may_read(site.url):
            return PageRead(site.key, False, site.url, "", "not a page the bank browser may open")
        if self.showing:
            return PageRead(site.key, False, site.url, "", "the sign-in window is open")
        browser = self.browser
        try:
            page = await browser.page(OWNER)
            await page.goto(site.url)
            found = await page.scan()
            for _ in range(LATE_TRIES):  # bank pages fill in their activity after the page itself has loaded
                if "$" in str(found.get("text", "")):
                    break
                await self._sleep(LATE_WAIT_S)
                found = await page.scan()
        finally:
            try:
                await browser.close_page(OWNER)
            except Exception:
                log.debug("Couldn't close the bank page", exc_info=True)
        url = str(found.get("url", "") or site.url)
        text = str(found.get("text", "") or "")
        items = [i for i in found.get("items", []) if isinstance(i, dict)]
        if not may_read(url):
            return PageRead(site.key, False, url, "", "the bank sent the page somewhere else")
        if _signed_out(url, text, items):
            return PageRead(site.key, False, url, "", "it wants a fresh sign-in")
        return PageRead(site.key, True, url, text)

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
