"""A bank's description of a transaction, made plain: who it was with (``merchant``) and what kind it is (``kind_of``).

Banks wrap the name in noise ("DEBIT CARD PURCHASE 11/05 SPOTIFY USA 8775552323 NY"): card and processor prefixes,
dates, store numbers, phone numbers, the city and state. What's left is the merchant, and its first words are the key
that groups one merchant's charges together (``merchant_key``), so recurring payments and odd charges can be found.

Kinds, by plain words only (no model): ``income`` (payroll, direct deposit, wages), ``transfer`` (between the user's
own accounts, or to and from Cash App), ``loan_payment`` (a student loan servicer), ``fee``, ``interest``, ``refund``,
``investment`` (buying or selling stock or bitcoin), and otherwise ``purchase`` (money out) or ``deposit`` (money in).
"""

from __future__ import annotations

import re

KINDS = (
    "purchase",
    "deposit",
    "income",
    "transfer",
    "loan_payment",
    "fee",
    "interest",
    "refund",
    "investment",
    "funding",  # a card payment that only paid for a Cash App payment already counted on the Cash App side
)
SPENDING_KINDS = frozenset({"purchase", "fee", "loan_payment"})

_PREFIXES = re.compile(
    r"^(?:(?:debit|credit|check)\s+card\s+(?:purchase|payment|transaction)|pos\s+(?:purchase|debit|withdrawal)|"
    r"purchase\s+authorized\s+on\s+\d{1,2}/\d{1,2}|recurring\s+(?:payment|purchase|debit)|"
    r"(?:ach|electronic|online)\s+(?:debit|credit|payment|withdrawal|deposit)|debit\s+purchase|pos|ach|"
    r"withdrawal|deposit|payment\s+to|payment\s+from|card\s+purchase|visa\s+(?:purchase|debit)|"
    r"(?:sq|tst|sp|py|pp|dd|ic|in|bt|fs|sms|paypal|cke|wpy|pmt)\s*\*)(?:\s+|(?<=\*)|$)\s*",
    re.I,
)
_NOISE = [
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),  # dates
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"[#*]\s*\d+"),  # store numbers
    re.compile(r"…\d{4}"),  # masked numbers
    re.compile(r"\b\d{3,}\b"),  # other long numbers (phone, terminal)
    re.compile(r"\b(?:x{2,}\d*|card\s+ending\s+in)\b", re.I),
    re.compile(r"https?://\S+|www\.\S+|\.com\b", re.I),
]
_STATES = (
    "al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne nv nh nj nm ny nc nd oh ok or pa "
    "ri sc sd tn tx ut vt va wa wv wi wy dc"
).split()
_STATE_TAIL = re.compile(r"\s+(?:" + "|".join(_STATES) + r")$", re.I)
_SPACES = re.compile(r"[^\w&'+ ]+|\s+")

_INCOME = re.compile(
    r"\b(?:payroll|direct\s*dep(?:osit)?|dir\s+dep|salary|wages?|paycheck|pay\s*roll|adp|gusto|paychex|"
    r"employer|treas\s+310|ssa\s+treas|va\s+benefit|dfas)\b",
    re.I,
)
_TRANSFER = re.compile(
    r"\b(?:transfer|xfer|trnsfr|from\s+savings|to\s+savings|from\s+checking|to\s+checking|cash\s*app|"
    r"square\s+inc|cash\s+in|cash\s+out|add\s+cash|standard\s+deposit|instant\s+deposit|round\s*ups?|"
    r"savings\s+(?:goal|transfer)|transfer\s+to\s+bank|transfer\s+from\s+bank)\b",
    re.I,
)
_LOAN = re.compile(
    r"\b(?:mohela|dept\s+(?:of\s+)?education|dept\s+ed|student\s+ln|studnt\s+ln|nelnet|aidvantage)\b", re.I
)
_FEE = re.compile(
    r"\b(?:fee|overdraft|nsf|insufficient\s+funds|returned\s+item|late\s+charge|service\s+charge|foreign\s+transaction)\b",
    re.I,
)
_INTEREST = re.compile(r"\b(?:interest\s+(?:paid|earned|payment|credit)|int\s+earned|dividend|apy)\b", re.I)
_REFUND = re.compile(r"\b(?:refund|return|reversal|credit\s+adjustment|chargeback)\b", re.I)
_INVEST = re.compile(r"\b(?:stock|bitcoin|btc|shares?|crypto|investing|buy\s+order|sell\s+order|etf)\b", re.I)
_CASH_APP = re.compile(r"\bcash\s*app\b|\bsquare\s+inc\b|\bsq\s*\*\s*cash\b", re.I)


def clean(description: str) -> str:
    """The description without the bank's wrapping, in lower case."""
    text = " ".join(str(description or "").split())
    for _ in range(3):  # "POS PURCHASE SQ *..." has two prefixes
        stripped = _PREFIXES.sub("", text)
        if stripped == text:
            break
        text = stripped
    for pattern in _NOISE:
        text = pattern.sub(" ", text)
    text = _SPACES.sub(" ", text).strip().lower()
    for _ in range(2):
        text = _STATE_TAIL.sub("", text).strip()
    return text


def merchant(description: str) -> str:
    """Who it was with, as words to say: "Spotify Usa"."""
    words = clean(description).split()[:4]
    return " ".join(w.capitalize() for w in words) or str(description or "").strip()[:40]


def merchant_key(description: str) -> str:
    """The merchant's first two words: the same merchant's charges share it."""
    words = [w for w in clean(description).split() if w not in ("the", "inc", "llc", "co")]
    return " ".join(words[:2]) or str(description or "").strip().lower()[:20]


def is_cash_app(description: str) -> bool:
    """Does a bank's line say the money went to or came from Cash App?"""
    return bool(_CASH_APP.search(str(description or "")))


def kind_of(description: str, cents: int, *, category: str = "", hint: str = "") -> str:
    """What kind of transaction it is, from its words (and a statement's own category or type, when it has one)."""
    text = f"{description} {category} {hint}"
    if _LOAN.search(text) and cents < 0:
        return "loan_payment"
    if _INVEST.search(f"{hint} {category}"):
        return "investment"
    if _FEE.search(text) and cents < 0:
        return "fee"
    if _INTEREST.search(text) and cents > 0:
        return "interest"
    if _TRANSFER.search(text):
        return "transfer"
    if cents > 0 and (_INCOME.search(text) or re.search(r"\b(?:income|paycheck|payroll)\b", category, re.I)):
        return "income"
    if cents > 0 and _REFUND.search(text):
        return "refund"
    return "purchase" if cents < 0 else "deposit"
