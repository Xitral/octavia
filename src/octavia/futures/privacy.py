"""The user's control over what Octavia Futures learns, keeps and says (docs/futures.md, "Privacy and control").

The user owns this model. The switches here decide:

* which sources are never recorded (``futures.exclude_sources`` in the config, e.g. ``[gmail]``);
* which domains are off-limits altogether: nothing from them is kept, learned from, experimented on or brought up
  (``futures.off_limits`` in the config, plus whatever the user said since: "don't learn anything about my health");
* where experiments may never happen ("don't experiment with my reminders");
* whether she speaks up on her own at all, and which kinds of remarks the user has muted;
* which hypotheses the user told her to forget, so the pattern finder doesn't simply form them again.

What the user says is kept in the ``controls`` table, so it survives restarts; the config's lists are a floor the
user's words add to (turning a domain back on only undoes what was said, never the config). Loosening a control
is a separate, confirmed action in the tools (``futures_controls``); tightening one never needs a yes.

``redact`` is the last line of defence for free text she keeps (a request's words, a trigger's message, a to-do's
title): anything that looks like a secret is cut out. ``classify_domain`` files what a text is about under a
protected domain (health, money, relationships, work) when its words say so, so "don't learn anything about my
health" reaches a to-do called "refill my prescription" too. It only knows the words it lists, and the controls
tool says so. ``FORGET_ASKS`` and ``LOOSENING`` decide which of the user's privacy actions are asked first.

This file is protected from self-improvement builds (evolve/policy.py), with its own copy of the secret patterns
rather than borrowing another module's: a build can't loosen the user's controls.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from octavia.futures.schema import Domain, Sensitivity
from octavia.futures.store import FuturesStore, dumps, loads

if TYPE_CHECKING:
    from octavia.futures.events import FuturesEvent

ALL = "*"  # "every domain", for the experiment switch
CACHE_S = 30.0

# Which of the user's privacy actions are asked first: deleting what she learned (except cancelling a reminder), and
# undoing a control the user set. Tightening a control never is.
FORGET_ASKS = frozenset({"hypothesis", "episode", "prediction", "goal", "source", "domain", "everything"})
LOOSENING = frozenset({"proactive_on", "allow_domain", "allow_experiments", "unmute"})

# A secret said out loud: "password is hunter2", "pin: 4411", "api key = sk_...". The value after the keyword is cut.
_KEYED_SECRET = re.compile(
    r"\b(passwords?|passcodes?|passphrases?|pin codes?|pins?|api[ _-]?keys?|secrets?|tokens?|access codes?|"
    r"security codes?|logins?|usernames?)(\s*(?:is|was|are|:|=)\s*|\s+)(\S+)",
    re.I,
)
# Things that are secrets by their shape, whatever is said around them.
_SHAPED_SECRETS = (
    re.compile(r"\b\d(?:[ -]?\d){12,18}\b"),  # card-number-like digit runs
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN-like
    re.compile(r"\b(?:sk|pk|ghp|gho|xox[abp])[-_][A-Za-z0-9]{10,}"),  # API keys
    re.compile(r"\b[A-Za-z0-9+/_-]{32,}\b"),  # long opaque tokens
)
_REDACTED = "[redacted]"

# Words that put a to-do or a request in a protected domain (lowercase, whole words).
# Words, and phrases, that put a to-do or a request in a protected domain (lowercase, whole words). Only words that
# mean the domain on their own: "date", "pay", "test", "shot", "offer" or "shift" say nothing by themselves ("due
# date", "pay attention", "chem test"), so they only count inside a phrase.
_DOMAIN_WORDS: dict[str, tuple[frozenset[str], tuple[str, ...]]] = {
    "health": (
        frozenset(
            "doctor doctors dentist dentists therapist therapy psychiatrist psychologist counselor counseling "
            "clinic hospital prescription prescriptions pharmacy meds medication medications pills vaccine "
            "vaccination checkup physio physiotherapy surgery bloodwork mri insulin zoloft prozac lexapro adderall "
            "ritalin ibuprofen x-ray".split()
        ),
        (
            "blood test",
            "flu shot",
            "covid shot",
            "booster shot",
            "doctor's appointment",
            "dentist appointment",
            "eye exam",
            "x ray",
            "sick day",
            "take my pill",
            "my anxiety",
            "my depression",
            "my diabetes",
            "my allergies",
            "my adhd",
            "my symptoms",
        ),
    ),
    "finance": (
        frozenset(
            "bank banking mortgage taxes irs invoice invoices paycheck debt venmo paypal zelle investing crypto "
            "bitcoin insurance fafsa tuition".split()
        ),
        (
            "pay rent",
            "the rent",
            "pay bills",
            "pay the bill",
            "credit card",
            "debit card",
            "bank transfer",
            "tax return",
            "student loan",
            "car payment",
            "my stocks",
        ),
    ),
    "relationships": (
        frozenset(
            "girlfriend boyfriend wife husband dating anniversary valentine valentine's breakup marriage wedding "
            "divorce".split()
        ),
        ("date night", "first date", "my partner", "my ex"),
    ),
    "employment": (
        frozenset("coworker coworkers recruiter timesheet payroll onboarding internship".split()),
        (
            "job interview",
            "job offer",
            "job application",
            "performance review",
            "my shift",
            "work shift",
            "my manager",
            "my boss",
            "my resume",
        ),
    ),
}
_WORD = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")


class Controls:
    """The user's switches, on top of the config's defaults."""

    def __init__(
        self,
        store: FuturesStore,
        *,
        proactive: bool = True,
        exclude_sources: Iterable[str] = (),
        off_limits: Iterable[str] = (),
    ) -> None:
        self.store = store
        # What's stored, read at most every CACHE_S: the controls are checked on every event, some from the event
        # loop, and a `octavia chat` beside the assistant may change them.
        self._cache: dict[str, tuple[float, Any]] = {}
        self._proactive_default = proactive
        self._excluded = frozenset(s.lower() for s in exclude_sources)
        self._config_off = frozenset(d.lower() for d in off_limits)

    # ------------------------------------------------------------------ storage
    def _get(self, key: str, default: Any) -> Any:
        cached = self._cache.get(key)
        if cached is None or time.monotonic() - cached[0] > CACHE_S:
            row = self.store.one("SELECT value FROM controls WHERE key=?", (key,))
            cached = (time.monotonic(), loads(row["value"], None) if row else None)
            self._cache[key] = cached
        return default if cached[1] is None else cached[1]

    def _set(self, key: str, value: Any, cause: str | None) -> None:
        self.store.execute(
            "INSERT INTO controls(key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, dumps(value), self.store.clock()),
        )
        self._cache[key] = (time.monotonic(), value)
        self.store.record_change(f"controls:{key}", "set", cause=cause, detail={"value": value})

    # ------------------------------------------------------------------ speaking up
    @property
    def proactive_enabled(self) -> bool:
        return bool(self._get("proactive", self._proactive_default))

    def set_proactive(self, on: bool, *, cause: str | None = None) -> None:
        self._set("proactive", bool(on), cause)

    def mute(self, kind: str, until: float, *, cause: str | None = None) -> None:
        """No more remarks of this kind (a proactive ``Candidate.kind``) until ``until`` (epoch seconds)."""
        muted = dict(self._get("muted", {}))
        muted[kind] = until
        self._set("muted", muted, cause)

    def unmute(self, kind: str, *, cause: str | None = None) -> None:
        muted = dict(self._get("muted", {}))
        if muted.pop(kind, None) is not None:
            self._set("muted", muted, cause)

    def muted(self, kind: str, now: float) -> bool:
        until = self._get("muted", {}).get(kind)
        return until is not None and now < float(until)

    # ------------------------------------------------------------------ domains
    @property
    def off_limits(self) -> frozenset[str]:
        return self._config_off | frozenset(self._get("off_limits", []))

    def set_off_limits(self, domain: str, off: bool, *, cause: str | None = None) -> None:
        """Put a domain off-limits, or take back an earlier "off-limits" (the config's list stays)."""
        domain = _domain(domain)
        said = set(self._get("off_limits", []))
        if off:
            said.add(domain)
        else:
            said.discard(domain)
        self._set("off_limits", sorted(said), cause)

    @property
    def no_experiments(self) -> frozenset[str]:
        return frozenset(self._get("no_experiments", []))

    def set_experiments(self, domain: str, allowed: bool, *, cause: str | None = None) -> None:
        """Allow or forbid experiments in a domain (``"*"``: everywhere)."""
        domain = ALL if domain == ALL else _domain(domain)
        banned = set(self._get("no_experiments", []))
        if allowed:
            banned.discard(domain)
        else:
            banned.add(domain)
        self._set("no_experiments", sorted(banned), cause)

    # ------------------------------------------------------------------ forgotten hypotheses
    def suppress_hypothesis(self, key: str, *, cause: str | None = None) -> None:
        keys = set(self._get("suppressed_hypotheses", []))
        keys.add(key)
        self._set("suppressed_hypotheses", sorted(keys), cause)

    def suppressed(self, key: str) -> bool:
        return key in self._get("suppressed_hypotheses", [])

    # ------------------------------------------------------------------ decisions
    def allows_source(self, source: str) -> bool:
        return source.lower() not in self._excluded

    def allows_event(self, event: FuturesEvent) -> bool:
        """May this event be kept at all? Not from an excluded source, not in an off-limits domain."""
        return self.allows_source(event.source) and (event.domain or "") not in self.off_limits

    def allows_learning(self, domain: str) -> bool:
        return domain not in self.off_limits

    def allows_experiment(self, domain: str) -> bool:
        return domain not in self.off_limits and ALL not in self.no_experiments and domain not in self.no_experiments

    def allows_proactive(self, domain: str) -> bool:
        return self.proactive_enabled and domain not in self.off_limits

    def snapshot(self) -> dict[str, Any]:
        """Everything the user has set, for "what are my privacy settings?" and the debug view."""
        return {
            "proactive": self.proactive_enabled,
            "off_limits": sorted(self.off_limits),
            "no_experiments": sorted(self.no_experiments),
            "excluded_sources": sorted(self._excluded),
            "muted": self._get("muted", {}),
            "forgotten_hypotheses": len(self._get("suppressed_hypotheses", [])),
        }


def _domain(value: str) -> str:
    value = value.strip().lower()
    try:
        return Domain(value).value
    except ValueError:
        known = ", ".join(d.value for d in Domain)
        raise ValueError(f"unknown domain {value!r}; the domains are {known}") from None


def _looks_like_credential(value: str) -> bool:
    """A digit, a symbol inside it, or case changing inside the word ("hUnTer"): not just a capital first letter."""
    core = value.strip(".,;:!?\"'()")
    if any(c.isdigit() for c in core) or (core and not core.isalpha()):
        return True
    inner = core[1:]
    return any(c.isupper() for c in inner) and any(c.islower() for c in inner)


# Keywords whose value after "is" is a secret whatever it looks like; for the others ("login", "username",
# "secret", "token": "my login was slow", "Secret Santa") the value has to look like a credential too.
_STRONG = re.compile(
    r"^(?:passwords?|passcodes?|passphrases?|pin codes?|pins?|api[ _-]?keys?|access codes?|security codes?)$", re.I
)


def _credential(match: re.Match[str]) -> bool:
    """Is this keyword-and-value a secret? After "is", ":" or "=" with a password-like keyword always; otherwise only
    when the value looks like a credential, so "the password reset page", "Fix Login Page" and "Secret Santa" keep
    their words."""
    if match.group(2).strip() and _STRONG.match(match.group(1)):
        return True
    return _looks_like_credential(match.group(3))


def looks_secret(text: str | None) -> bool:
    """Does ``text`` seem to hold a secret: a password or key said out loud, a card number, a token?"""
    text = text or ""
    return any(_credential(m) for m in _KEYED_SECRET.finditer(text)) or any(p.search(text) for p in _SHAPED_SECRETS)


def redact(text: str | None, limit: int = 200) -> str:
    """``text`` fit to keep: whitespace collapsed, every secret in it (the value after "password is", a card number,
    a key) replaced by "[redacted]", and cut to ``limit`` characters."""
    text = " ".join((text or "").split())
    text = _KEYED_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}" if _credential(m) else m.group(0), text)
    for pattern in _SHAPED_SECRETS:
        text = pattern.sub(_REDACTED, text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def classify_domain(*texts: str | None) -> str | None:
    """The protected domain these texts are about (``"health"``, ``"finance"``, ``"relationships"``,
    ``"employment"``) when one of its words or phrases is in them, else None. Deliberately simple: a word list,
    never a model, so it can be read and checked. What it doesn't list, it doesn't catch."""
    spoken = [" ".join(_WORD.findall((text or "").lower())) for text in texts]
    words = {w for text in spoken for w in text.split()}
    padded = [f" {text} " for text in spoken]
    for domain, (listed, phrases) in _DOMAIN_WORDS.items():
        if words & listed or any(f" {phrase} " in text for phrase in phrases for text in padded):
            return domain
    return None


# Domains nothing is filed under except by ``classify_domain`` (or not at all): the controls tool says so when the
# user puts one off-limits, rather than promising more than the word list can do.
WORD_LISTED = frozenset(_DOMAIN_WORDS)


def speakable(sensitivity: str | Sensitivity) -> bool:
    """May something this sensitive be said out loud unprompted? Not a grade, not someone else's message."""
    return Sensitivity(sensitivity) is not Sensitivity.SENSITIVE
