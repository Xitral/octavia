"""Gmail through Google's API, writing: a draft, a reply in an email's thread, and a new email, each after a yes.

They use the sign-ins of connect_gmail_account (``gmail_api``), which asks for ``gmail.compose`` next to
``gmail.readonly``: drafts and sending, nothing changed or deleted. An account connected before that, or whose user
unticked the permission, is still searched; these tools then say to connect it again, and never forget its sign-in
over it (``ComposeApi``).

Sending can't be taken back, so every send is asked about, the question naming the account it's from, who it goes to,
the subject and the whole text. Each send is its own question: a yes to one email never covers the next one in the
same turn (``inspect_risk`` gives every call its own scope). A reply is worked out before the question (to whom, the
subject, which thread) from the email search_gmail found, and kept, so the email sent is exactly the one the user
heard and said yes to.
"""

from __future__ import annotations

import base64
import itertools
import logging
import re
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Any

import httpx
from pydantic import Field, field_validator

from octavia.tools.base import RiskFinding, RiskLevel, Tool, ToolArgs, ToolContext, ToolResult
from octavia.tools.extensions.gmail_api import (
    CLIENT_DIR,
    NO_WRITING,
    GmailApi,
    Gone,
    GoogleAuth,
    GoogleError,
    NotConnected,
    _api_problem,
    _headers,
    _http,
    can_write,
    load_client,
    setup_hint,
)

log = logging.getLogger(__name__)

MAX_BODY = 5000
MAX_RECIPIENTS = 20
REMEMBER_S = 600.0  # how long the email worked out before the question is the one written
ORIGINAL_HEADERS = ["From", "Reply-To", "To", "Cc", "Subject", "Message-ID", "References"]
NOT_CONNECTED = (
    "No Gmail account is connected to write from yet: use connect_gmail_account so the user can sign in once in the "
    "browser."
)

_ADDRESS = re.compile(r"^[^@\s<>,;:\"()\[\]]+@[^@\s<>,;:\"()\[\]]+\.[^@\s<>,;:\"()\[\]]+$")
_REPLY_PREFIX = re.compile(r"^\s*re\s*:", re.IGNORECASE)
_SPECIAL = re.compile(r"[,;:<>@()\[\]\"\\.]")
_sends = itertools.count(1)


class ComposeApi(GmailApi):
    """Gmail for writing. A sign-in without the permission to write still searches, so it's kept, not forgotten."""

    def problem(self, response: httpx.Response) -> GoogleError:
        status, text = response.status_code, response.text
        if status == 403 and ("insufficientPermissions" in text or "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in text):
            return GoogleError(NO_WRITING)
        if status == 400:
            return GoogleError("Gmail refused the email (HTTP 400); an address in it may be wrong")
        return _api_problem(response)


# --------------------------------------------------------------------------- the email


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _person(name: str, address: str) -> str:
    name = _one_line(name).replace('"', "'")
    if not name:
        return address
    return f'"{name}" <{address}>' if _SPECIAL.search(name) else f"{name} <{address}>"


def _people(*values: str) -> list[tuple[str, str]]:
    return [(n, a.strip()) for n, a in getaddresses(list(values)) if _ADDRESS.match(a.strip())]


def _unique(people: list[tuple[str, str]], skip: set[str]) -> tuple[str, ...]:
    """The people, each once, but those whose address is in ``skip`` (lowercase)."""
    seen = set(skip)
    out: list[str] = []
    for name, address in people:
        if address.lower() not in seen:
            seen.add(address.lower())
            out.append(_person(name, address))
    return tuple(out)


def _address(person: str) -> str:
    return parseaddr(person)[1].lower()


@dataclass(frozen=True)
class Outgoing:
    """An email to write: from ``account``; in a thread, as a reply, when ``thread_id`` is set."""

    account: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    body: str
    thread_id: str = ""
    in_reply_to: str = ""
    references: str = ""

    def raw(self) -> str:
        """The email in RFC 2822 form, base64url, as Gmail takes it."""
        message = EmailMessage()
        message["From"] = self.account
        if self.to:
            message["To"] = ", ".join(self.to)
        if self.cc:
            message["Cc"] = ", ".join(self.cc)
        message["Subject"] = self.subject
        if self.in_reply_to:
            message["In-Reply-To"] = self.in_reply_to
            message["References"] = self.references or self.in_reply_to
        message.set_content(self.body)
        return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    def resource(self) -> dict[str, Any]:
        found: dict[str, Any] = {"raw": self.raw()}
        if self.thread_id:
            found["threadId"] = self.thread_id
        return found

    def said(self) -> str:
        """Everything the user approves: from where, to whom, the subject and the whole text."""
        to = ", ".join(self.to) or "no one yet"
        cc = f", copying {', '.join(self.cc)}" if self.cc else ""
        subject = f"“{self.subject}”" if self.subject else "(no subject)"
        return f"from {self.account} to {to}{cc}, subject {subject}, saying: “{self.body}”"


def reply_to(account: str, original: Any, reply_all: bool, cc: list[str], body: str) -> Outgoing | str:
    """The reply to ``original`` (a ``users.messages.get`` answer, ``format=metadata``) in its thread, or why there
    can't be one. A reply to the user's own email goes to whom it went to."""
    if not isinstance(original, dict) or not original.get("threadId"):
        return "Gmail sent an answer I couldn't read"
    headers = _headers(original.get("payload") if isinstance(original.get("payload"), dict) else {})
    mine = {account.lower()}
    senders = _people(headers.get("reply-to") or headers.get("from", ""))
    addressed = _people(headers.get("to", ""))
    copied = _people(headers.get("cc", ""))
    if senders and all(a.lower() in mine for _, a in senders):
        to, others = _unique(addressed, mine), copied
    else:
        to, others = _unique(senders, mine), addressed + copied
    if not to:
        return "That email has no address to reply to."
    given = _people(*cc)
    everyone = (others if reply_all else []) + given
    copies = _unique(everyone, mine | {_address(t) for t in to})
    subject = _one_line(headers.get("subject", ""))
    if not _REPLY_PREFIX.match(subject):
        subject = f"Re: {subject}".strip()
    message_id = _one_line(headers.get("message-id", ""))
    return Outgoing(
        account=account,
        to=to,
        cc=copies,
        subject=subject,
        body=body,
        thread_id=str(original["threadId"]),
        in_reply_to=message_id,
        references=_one_line(f"{headers.get('references', '')} {message_id}"),
    )


# --------------------------------------------------------------------------- arguments


def _check_addresses(values: list[str]) -> list[str]:
    checked: list[str] = []
    for value in values:
        value = _one_line(value)
        name, address = parseaddr(value)
        if not _ADDRESS.match(address):
            raise ValueError(f"{value!r} isn't an email address: give the real address, never a guess")
        checked.append(_person(name, address))
    return checked


class _WriteArgs(ToolArgs):
    cc: list[str] = Field(default_factory=list, max_length=MAX_RECIPIENTS)
    body: str = Field(
        min_length=1,
        max_length=MAX_BODY,
        description="Full text incl. greeting and sign-off.",
    )
    account: str | None = Field(
        default=None,
        description="Sender account (address or part, e.g. 'school.edu'), if several.",
    )

    @field_validator("cc")
    @classmethod
    def _real_cc(cls, values: list[str]) -> list[str]:
        return _check_addresses(values)


class SendGmailArgs(_WriteArgs):
    to: list[str] = Field(
        min_length=1,
        max_length=MAX_RECIPIENTS,
        description="Real addresses from the user or search_gmail; never guessed.",
    )
    subject: str = Field(min_length=1, max_length=300)

    @field_validator("to")
    @classmethod
    def _real_to(cls, values: list[str]) -> list[str]:
        return _check_addresses(values)

    @field_validator("subject")
    @classmethod
    def _one_line_subject(cls, value: str) -> str:
        return _one_line(value)


class ReplyGmailArgs(_WriteArgs):
    email_id: str = Field(
        max_length=200,
        pattern=r"^[A-Za-z0-9_-]+$",  # it goes in the request's path
        description="From search_gmail.",
    )
    reply_all: bool = Field(default=False)


class DraftGmailArgs(_WriteArgs):
    to: list[str] = Field(
        default_factory=list,
        max_length=MAX_RECIPIENTS,
        description="For a reply, omit to answer the sender.",
    )
    subject: str = Field(default="", max_length=300)
    reply_to_email_id: str | None = Field(
        default=None,
        max_length=200,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="An email_id from search_gmail, to reply in its thread.",
    )
    reply_all: bool = Field(default=False)

    @field_validator("to")
    @classmethod
    def _real_to(cls, values: list[str]) -> list[str]:
        return _check_addresses(values)

    @field_validator("subject")
    @classmethod
    def _one_line_subject(cls, value: str) -> str:
        return _one_line(value)


# --------------------------------------------------------------------------- tools


def _failure(address: str, exc: GoogleError) -> str:
    if isinstance(exc, NotConnected):
        return f"{address}: {exc}; use connect_gmail_account to connect it again"
    return f"{address}: {exc}"


def _pick(accounts: list[str], wanted: str | None) -> tuple[list[str], str | None]:
    """The connected accounts ``wanted`` names (all of them when it's None), or why there are none."""
    if not wanted:
        return accounts, None
    lowered = wanted.lower()
    chosen = [a for a in accounts if a == lowered] or [a for a in accounts if lowered in a]
    if not chosen:
        return [], (
            f"No connected Gmail account matches {wanted!r}; connected: {', '.join(accounts)}. Use "
            "connect_gmail_account to add it."
        )
    return chosen, None


def _one(accounts: list[str]) -> str | None:
    if len(accounts) > 1:
        return (
            f"Several Gmail accounts are connected ({', '.join(accounts)}): ask the user which one it's from, then "
            "call again with its account."
        )
    return None


class _GmailWriter(Tool):
    """What the three tools share: the sign-in, the email worked out before the question and kept for the run."""

    risk = RiskLevel.CONFIRM
    category = "email"
    timeout_s = 45.0

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, client_dir: Path | None = None) -> None:
        self._transport = transport
        self._client_dir = client_dir if client_dir is not None else CLIENT_DIR
        self._ready: dict[str, tuple[float, GoogleAuth, Outgoing]] = {}

    async def _prepare(self, args: Any, auth: GoogleAuth, accounts: list[str]) -> Outgoing | str:
        """The email to write, or why it can't be."""
        raise NotImplementedError

    async def _write(self, api: ComposeApi, email: Outgoing) -> Any:
        raise NotImplementedError

    def _phrase(self, email: Outgoing) -> str:
        raise NotImplementedError

    def _done(self, email: Outgoing) -> str:
        raise NotImplementedError

    async def _resolve(self, args: Any, ctx: ToolContext) -> tuple[GoogleAuth, Outgoing] | str:
        key = args.model_dump_json()
        if (kept := self._ready.get(key)) is not None and time.monotonic() - kept[0] < REMEMBER_S:
            return kept[1], kept[2]
        client = load_client(self._client_dir)
        if client is None:
            return setup_hint(self._client_dir)
        auth = GoogleAuth(ctx, client, self._transport)
        saved = await auth.accounts()
        if not saved:
            gone = await auth.unavailable("connect_gmail_account")
            return f"No Gmail account is connected to write from now: {gone}" if gone else NOT_CONNECTED
        email = await self._prepare(args, auth, sorted(saved))
        if isinstance(email, str):
            return email
        if can_write(saved.get(email.account, {})) is False:
            return f"{email.account}: {NO_WRITING}."
        self._ready[key] = (time.monotonic(), auth, email)
        return auth, email

    async def precheck(self, args: Any, ctx: ToolContext) -> str | None:
        resolved = await self._resolve(args, ctx)
        return resolved if isinstance(resolved, str) else None

    async def describe_call(self, args: Any, ctx: ToolContext) -> str:
        resolved = await self._resolve(args, ctx)
        if isinstance(resolved, str):
            return f"write an email from Gmail, saying: “{args.body}”"
        return self._phrase(resolved[1])

    async def run(self, args: Any, ctx: ToolContext) -> ToolResult:
        resolved = await self._resolve(args, ctx)
        self._ready.pop(args.model_dump_json(), None)  # the one the user said yes to, used once
        if isinstance(resolved, str):
            return ToolResult.error(resolved)
        auth, email = resolved
        async with _http(self._transport) as http:
            try:
                answer = await self._write(ComposeApi(auth, email.account, http), email)
            except GoogleError as exc:
                log.warning("Couldn't write a Gmail email: %s", exc)
                return ToolResult.error(f"Nothing was sent or saved. {_failure(email.account, exc)}")
        found = answer if isinstance(answer, dict) else {}
        return ToolResult.success(
            self._done(email),
            data={"account": email.account, "id": str(found.get("id") or ""), "to": list(email.to)},
        )

    def _new(self, args: SendGmailArgs | DraftGmailArgs, accounts: list[str]) -> Outgoing | str:
        chosen, problem = _pick(accounts, args.account)
        if problem or (problem := _one(chosen)):
            return problem
        return Outgoing(account=chosen[0], to=tuple(args.to), cc=tuple(args.cc), subject=args.subject, body=args.body)

    async def _reply(
        self, auth: GoogleAuth, accounts: list[str], wanted: str | None, email_id: str, reply_all: bool, args: Any
    ) -> Outgoing | str:
        """The reply to the email ``email_id``, looked for in the account named (else in each connected one)."""
        chosen, problem = _pick(accounts, wanted)
        if problem:
            return problem
        failures: list[str] = []
        params = {"format": "metadata", "metadataHeaders": ORIGINAL_HEADERS}
        async with _http(self._transport) as http:
            for address in chosen:  # one at a time: a refreshed token is saved over the others' entry
                try:
                    original = await ComposeApi(auth, address, http).get(f"/messages/{email_id}", params)
                except Gone:
                    continue
                except GoogleError as exc:
                    failures.append(_failure(address, exc))
                    continue
                return reply_to(address, original, reply_all, args.cc, args.body)
        if failures:
            return "I couldn't find the email to answer. " + " ".join(failures)
        return f"No email with the email_id {email_id} in {', '.join(chosen)}: find it again with search_gmail."


class SendGmail(_GmailWriter):
    name = "send_gmail"
    description = (
        "Send a new email from a connected Gmail account. The user is always asked first (hears recipients, "
        "subject, text); nothing is sent without their yes. To answer an email use reply_gmail; to write one "
        "without sending it, use create_gmail_draft."
    )
    Args = SendGmailArgs

    async def inspect_risk(self, args: SendGmailArgs, ctx: ToolContext) -> RiskFinding:
        return RiskFinding(RiskLevel.CONFIRM, scope=f"send#{next(_sends)}", reason="can't be unsent")

    async def _prepare(self, args: SendGmailArgs, auth: GoogleAuth, accounts: list[str]) -> Outgoing | str:
        return self._new(args, accounts)

    async def _write(self, api: ComposeApi, email: Outgoing) -> Any:
        return await api.request("POST", "/messages/send", body=email.resource())

    def _phrase(self, email: Outgoing) -> str:
        return f"send an email {email.said()}"

    def _done(self, email: Outgoing) -> str:
        return f"Sent the email “{email.subject}” from {email.account} to {', '.join(email.to)}."


class ReplyGmail(_GmailWriter):
    name = "reply_gmail"
    description = (
        "Reply in an email's thread, from its account. The user is always asked first (hears recipients, subject, "
        "text); nothing is sent without their yes. To write a reply without sending it, use create_gmail_draft "
        "with reply_to_email_id."
    )
    Args = ReplyGmailArgs

    async def inspect_risk(self, args: ReplyGmailArgs, ctx: ToolContext) -> RiskFinding:
        return RiskFinding(RiskLevel.CONFIRM, scope=f"send#{next(_sends)}", reason="can't be unsent")

    async def _prepare(self, args: ReplyGmailArgs, auth: GoogleAuth, accounts: list[str]) -> Outgoing | str:
        return await self._reply(auth, accounts, args.account, args.email_id, args.reply_all, args)

    async def _write(self, api: ComposeApi, email: Outgoing) -> Any:
        return await api.request("POST", "/messages/send", body=email.resource())

    def _phrase(self, email: Outgoing) -> str:
        return f"send a reply {email.said()}"

    def _done(self, email: Outgoing) -> str:
        return f"Sent the reply “{email.subject}” from {email.account} to {', '.join(email.to)}, in its thread."


class CreateGmailDraft(_GmailWriter):
    name = "create_gmail_draft"
    description = (
        "Save a Gmail draft (not sent). The user is asked first. Use when they say 'draft' or want to check it first."
    )
    Args = DraftGmailArgs

    async def _prepare(self, args: DraftGmailArgs, auth: GoogleAuth, accounts: list[str]) -> Outgoing | str:
        if not args.reply_to_email_id:
            return self._new(args, accounts)
        found = await self._reply(auth, accounts, args.account, args.reply_to_email_id, args.reply_all, args)
        if isinstance(found, str) or not args.to:
            return found
        to = tuple(args.to)
        return Outgoing(
            account=found.account,
            to=to,
            cc=tuple(c for c in found.cc if _address(c) not in {_address(t) for t in to}),
            subject=args.subject or found.subject,
            body=found.body,
            thread_id=found.thread_id,
            in_reply_to=found.in_reply_to,
            references=found.references,
        )

    async def _write(self, api: ComposeApi, email: Outgoing) -> Any:
        return await api.request("POST", "/drafts", body={"message": email.resource()})

    def _phrase(self, email: Outgoing) -> str:
        return f"save a Gmail draft (not sent) {email.said()}"

    def _done(self, email: Outgoing) -> str:
        to = f" to {', '.join(email.to)}" if email.to else ""
        return f"Saved the draft{to} in the Drafts of {email.account}; it isn't sent."


TOOLS = [CreateGmailDraft, ReplyGmail, SendGmail]
