"""What screen history never keeps, and how what it does keep is cleaned first. Plain rules, no model.

Nothing at all is kept (not even that the window was open) from:

* an app on the exclusion list: password managers, Windows' own sign-in and credential prompts, and whatever the
  user adds (``excluded_app``);
* a private or incognito browser window (``private_title``, and ``PRIVATE_MARKERS`` for the browser's own badge,
  which ``capture`` looks for in the window's toolbar);
* a banking or payment site (``excluded_site``: ``BANK_HOSTS`` and the user's own sites, matched by host) or a window
  whose title says it's a bank (``BANK_WORDS``).

From everything else, ``clean_lines`` drops password fields (``capture`` never reads them) and every line that looks
like a password, a key or a token, and runs the rest through Octavia Futures' ``redact``, which cuts out a password
said in a sentence, a card number or a key. ``clean_url`` keeps only the scheme, host and path of an address: the
query string and fragment, where sign-in tokens and search words live, are dropped.

This file is protected from self-improvement builds (evolve/policy.py), like futures/privacy.py.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from octavia.futures.privacy import looks_secret as _says_secret
from octavia.futures.privacy import redact

# Process image names, lower case. Password managers, and Windows' own prompts for a password, a PIN or an admin's yes.
EXCLUDED_APPS: frozenset[str] = frozenset(
    {
        "1password.exe",
        "agilebits.onepassword.desktop.exe",
        "bitwarden.exe",
        "keepass.exe",
        "keepassxc.exe",
        "lastpass.exe",
        "dashlane.exe",
        "enpass.exe",
        "nordpass.exe",
        "roboform.exe",
        "protonpass.exe",
        "proton pass.exe",
        "keeper.exe",
        "keeperpasswordmanager.exe",
        "credentialuibroker.exe",
        "consent.exe",
        "logonui.exe",
        "lockapp.exe",
        "securityhealthhost.exe",
    }
)

# Banking, payment and brokerage sites, by host (a subdomain matches too). The user adds their own (``excluded_site``).
BANK_HOSTS: frozenset[str] = frozenset(
    {
        "chase.com",
        "bankofamerica.com",
        "wellsfargo.com",
        "capitalone.com",
        "citi.com",
        "citibank.com",
        "usbank.com",
        "pnc.com",
        "truist.com",
        "tdbank.com",
        "ally.com",
        "discover.com",
        "americanexpress.com",
        "navyfederal.org",
        "usaa.com",
        "sofi.com",
        "chime.com",
        "schwab.com",
        "fidelity.com",
        "vanguard.com",
        "etrade.com",
        "robinhood.com",
        "coinbase.com",
        "paypal.com",
        "venmo.com",
        "cash.app",
        "mohela.com",
        "studentaid.gov",
        "wise.com",
        "revolut.com",
        "mint.intuit.com",
        "creditkarma.com",
        "experian.com",
        "equifax.com",
        "transunion.com",
        "irs.gov",
        "ssa.gov",
    }
)

# Window-title words that mean a bank or a credit union, whatever its address.
BANK_WORDS = re.compile(r"(?i)\b(?:bank|banking|credit union|federal credit|brokerage)\b")

# What a browser shows in a private window: in its title (Firefox, Edge) or as a badge in its toolbar (Chrome, Brave,
# Edge, Opera), which ``capture`` reads by name.
_PRIVATE_TITLE = re.compile(r"(?i)\b(?:inprivate|incognito|private browsing|private window)\b|\(private\)")
PRIVATE_MARKERS: frozenset[str] = frozenset(
    {"incognito", "inprivate", "private", "private browsing", "private window", "you've gone incognito", "tor"}
)

BROWSERS: frozenset[str] = frozenset(
    {
        "chrome.exe",
        "msedge.exe",
        "firefox.exe",
        "brave.exe",
        "opera.exe",
        "opera_gx.exe",
        "vivaldi.exe",
        "arc.exe",
        "librewolf.exe",
        "waterfox.exe",
        "zen.exe",
        "floorp.exe",
        "tor.exe",
        "thorium.exe",
        "chromium.exe",
        "browser.exe",  # Yandex Browser
        "duckduckgo.exe",
        "whale.exe",
        "maxthon.exe",
        "slimjet.exe",
        "iron.exe",
        "sidekick.exe",
        "palemoon.exe",
        "seamonkey.exe",
        "mullvadbrowser.exe",
    }
)
TOR_BROWSERS: frozenset[str] = frozenset({"tor.exe"})  # every window of it is private

MAX_LINE_CHARS = 1000
_WORD = re.compile(r"\S+")
_KEYLIKE = re.compile(r"^[A-Za-z0-9_\-+/=.]{20,}$")
_HAS_LETTER_AND_DIGIT = re.compile(r"(?=.*[A-Za-z])(?=.*\d)")
_LINK_OR_PATH = re.compile(r"^(?:https?://|www\.|[A-Za-z]:[\\/]|\\\\|~?/)", re.IGNORECASE)
_EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(?:\.[\w-]+)+$")
_ONE_TIME_CODE = re.compile(
    r"(?i)\b(?:verification|security|one[- ]time|sign[- ]in|login|2fa|otp|auth(?:entication)?)\s+code\b"
)


def norm_app(process: str) -> str:
    return process.strip().lower()


def host_of(url: str) -> str:
    """The host of ``url`` in lower case without "www."; "" when there's none."""
    url = url.strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _host_matches(host: str, listed: Iterable[str]) -> bool:
    """``host`` is a listed site or one of its subdomains; a listed bare name ("mychart") matches any host with
    that name as one of its labels (mychart.org, my.mychart.com), never a part of a label (notmychart.org)."""
    labels = host.split(".")
    for site in listed:
        if not site:
            continue
        if "." in site:
            if host == site or host.endswith("." + site):
                return True
        elif site in labels:
            return True
    return False


def clean_site(value: str) -> str:
    """What the user names as a site ("Chase.com", "https://www.chase.com/login"), as a host to match ("chase.com");
    a bare name ("MyChart") stays a name, matched against every label of a host."""
    value = value.strip().lower()
    if "." not in value:
        return value
    return host_of(value) or value


def excluded_app(process: str, user_apps: Iterable[str] = ()) -> bool:
    """Is this process on the exclusion list (the built-in one or the user's)? The user's entries match the image name
    with or without ".exe", or a word of it ("1password" matches "1Password.exe")."""
    name = norm_app(process)
    if not name:
        return False
    if name in EXCLUDED_APPS:
        return True
    stem = name[:-4] if name.endswith(".exe") else name
    for entry in user_apps:
        entry = norm_app(entry)
        entry = entry[:-4] if entry.endswith(".exe") else entry
        if entry and (entry == stem or entry in stem):
            return True
    return False


def excluded_site(url: str, title: str = "", user_sites: Iterable[str] = ()) -> bool:
    """Is this a banking or payment site (by its host), one the user excluded, or a window titled as a bank's?"""
    host = host_of(url)
    if host and (_host_matches(host, BANK_HOSTS) or _host_matches(host, [clean_site(s) for s in user_sites])):
        return True
    return bool(title and BANK_WORDS.search(title))


def private_title(title: str, process: str = "") -> bool:
    """Does the window's title (or the browser itself) say it's a private window?"""
    return norm_app(process) in TOR_BROWSERS or bool(_PRIVATE_TITLE.search(title or ""))


def is_private_marker(name: str) -> bool:
    return (name or "").strip().lower() in PRIVATE_MARKERS


def clean_url(url: str) -> str:
    """``url`` without what can carry a secret: only scheme, host, port and path ("https://example.com/a/b"); "" when
    it isn't a web address. Sign-in names and passwords in it ("user:pass@") are dropped too."""
    url = (url or "").strip()
    if not url or any(ch.isspace() for ch in url):
        return ""
    if "://" not in url:
        if "." not in url.split("/")[0]:
            return ""  # a search typed into the address bar, not an address
        url = "https://" + url
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not host:
        return ""
    netloc = host + (f":{port}" if port else "")
    path = parts.path if parts.path not in ("", "/") else ""
    return f"{parts.scheme}://{netloc}{path}"


def secret_line(line: str) -> bool:
    """A line better not kept at all: it says or carries a secret, or it is a lone word shaped like a password or
    a key ("Tr0ub4dor&3", a 40-character token), or a one-time code with its label."""
    if _says_secret(line) or _ONE_TIME_CODE.search(line):
        return True
    words = _WORD.findall(line)
    if len(words) != 1:
        return False
    word = words[0]
    if _LINK_OR_PATH.match(word) or _EMAIL.match(word):
        return False
    if _KEYLIKE.match(word) and _HAS_LETTER_AND_DIGIT.match(word):
        return True
    kinds = (
        any(c.islower() for c in word)
        + any(c.isupper() for c in word)
        + any(c.isdigit() for c in word)
        + any(not c.isalnum() for c in word)
    )
    return len(word) >= 8 and any(c.isdigit() for c in word) and kinds >= 3


def clean_lines(lines: Iterable[str]) -> list[str]:
    """The lines worth keeping, in order and each once: blank lines, single characters and secret-looking lines
    dropped, and every remaining one redacted and cut to ``MAX_LINE_CHARS``."""
    kept: list[str] = []
    seen: set[str] = set()
    for raw in lines:
        line = " ".join(str(raw).split())
        if len(line) < 2 or secret_line(line):
            continue
        line = redact(line, MAX_LINE_CHARS)
        if line and line not in seen:
            seen.add(line)
            kept.append(line)
    return kept


def clean_title(title: str) -> str:
    return redact(title, 300)
