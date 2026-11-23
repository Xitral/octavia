"""Tools for the playbooks her screen and web tasks keep on each app and site (``octavia.playbooks``): what she has
learned about one, and forgetting it.

Reading them is safe. Forgetting one is asked, like ``forget``: it deletes the notes and their history for good.
"""

from __future__ import annotations

import time

from pydantic import Field

from octavia.playbooks.book import Place, Playbook, Playbooks, same_site
from octavia.tools.base import RiskLevel, Tool, ToolArgs, ToolContext, ToolResult

_UNAVAILABLE = "Playbooks aren't available right now (they start with the voice assistant, with long-term memory on)."


def _books(ctx: ToolContext) -> Playbooks | None:
    books = ctx.extras.get("playbooks")
    return books if isinstance(books, Playbooks) else None


def _match(books: list[Playbook], name: str) -> list[Playbook]:
    """The playbooks ``name`` means: the app or site by its key, a host's site name, or a part of the key."""
    wanted = " ".join(name.casefold().replace("www.", "").split())
    if not wanted:
        return []
    exact = [b for b in books if b.place.key == wanted]
    if exact:
        return exact
    named = [b for b in books if same_site(b.place, Place("site", wanted)) or b.place.label() == wanted]
    return named or [b for b in books if wanted in b.place.key]


def _line(book: Playbook) -> str:
    day = time.strftime("%b %d", time.localtime(book.updated_at))
    kind = "site" if book.place.kind == "site" else "app"
    return f"{book.place.key} ({kind}, {len(book.notes)} note{'s' if len(book.notes) != 1 else ''}, updated {day})"


class AppPlaybooksArgs(ToolArgs):
    app: str = Field(
        default="", max_length=100, description="The app or site ('PowerPoint', 'datacamp'). Empty: list them."
    )


class AppPlaybooks(Tool):
    name = "app_playbooks"
    description = (
        "What your screen and web tasks have learned about an app or website (where things are, what worked, what "
        "failed and the fix), or the list of apps and sites you have notes on."
    )
    Args = AppPlaybooksArgs
    risk = RiskLevel.SAFE
    category = "tasks"

    async def run(self, args: AppPlaybooksArgs, ctx: ToolContext) -> ToolResult:
        books = _books(ctx)
        if books is None:
            return ToolResult.error(_UNAVAILABLE)
        found = await books.all()
        if not args.app:
            if not found:
                return ToolResult.success("No playbooks yet: they're written after screen and web tasks.")
            return ToolResult.success("Playbooks:\n" + "\n".join(_line(book) for book in found))
        matched = _match(found, args.app)
        if not matched:
            return ToolResult.success(f"No playbook for “{args.app}” yet.")
        parts = [f"{_line(book)}:\n" + "\n".join(f"- {note}" for note in book.notes) for book in matched[:3]]
        return ToolResult.success("\n\n".join(parts), data={"ids": [book.id for book in matched[:3]]})


class ForgetAppPlaybookArgs(ToolArgs):
    app: str = Field(min_length=1, max_length=100, description="The app or site whose notes to delete.")


class ForgetAppPlaybook(Tool):
    name = "forget_app_playbook"
    description = "Delete what your tasks learned about one app or website (its playbook and the earlier versions)."
    Args = ForgetAppPlaybookArgs
    risk = RiskLevel.CONFIRM  # gone for good, like forget
    category = "tasks"

    async def describe_call(self, args: ForgetAppPlaybookArgs, ctx: ToolContext) -> str:
        return f"forget my notes on {args.app}"

    async def run(self, args: ForgetAppPlaybookArgs, ctx: ToolContext) -> ToolResult:
        books = _books(ctx)
        if books is None:
            return ToolResult.error(_UNAVAILABLE)
        matched = _match(await books.all(), args.app)
        if not matched:
            return ToolResult.error(f"There's no playbook for “{args.app}”; app_playbooks lists them.")
        if len(matched) > 1:
            names = ", ".join(book.place.key for book in matched[:5])
            return ToolResult.error(f"“{args.app}” could be {names}: say which one.")
        book = matched[0]
        if not await books.forget(book.place):
            return ToolResult.error(f"The playbook for {book.place.key} was already gone.")
        return ToolResult.success(f"Forgot the playbook for {book.place.key}.")


TOOLS = [AppPlaybooks, ForgetAppPlaybook]
