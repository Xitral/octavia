"""Playbooks hooked to screen and web tasks (``App._start_playbooks``).

``attach`` is called each time a task's brain is made (a screen task paused and picked up again gets a new one): its
prompt gets the playbooks of the apps and sites its instruction names, and its executor is wrapped
(``trace.PlaybookExecutor``) so its steps are written down and it's handed the playbook of any other place it gets
to. ``finisher`` gives the task manager's ``finished`` hook, which queues the task's write-up (``writer``).

With ``skills`` (``octavia.skills.wiring.TaskSkills``) the same two hooks also give the task its skills: the executor
is wrapped once more (``skills.runner.SkillExecutor``, outside the playbooks' wrapper, so a skill's steps are written
down too), the prompt lists the skills it can use, and the end of the task is passed on. Skills work with playbooks
turned off too, but without knowing where a task was they can only be used then: nothing is proposed or noticed
(a step somewhere private couldn't be told apart).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from octavia.playbooks.book import Playbooks, block
from octavia.playbooks.trace import PlaybookExecutor, Trace, screen_where, web_where
from octavia.playbooks.writer import Job, PlaybookWriter

log = logging.getLogger(__name__)

MAX_TRACES = 40  # tasks whose record is kept until they end (a stopped one never does)
Prompt = Callable[[], Awaitable[str]]


class TaskPlaybooks:
    def __init__(
        self,
        books: Playbooks,
        writer: PlaybookWriter,
        *,
        enabled: Callable[[], bool] = lambda: True,
        session: Callable[[], str | None] = lambda: None,
        user_apps: Callable[[], Iterable[str]] = lambda: (),
        user_sites: Callable[[], Iterable[str]] = lambda: (),
        foreground: Callable[[], Any] | None = None,
        skills: Any = None,
    ) -> None:
        self.books = books
        self.writer = writer
        self._enabled = enabled
        self._session = session
        self._user_apps = user_apps
        self._user_sites = user_sites
        self._foreground = foreground
        self.skills = skills
        self.traces: dict[tuple[str, int], Trace] = {}

    def trace(self, kind: str, task_id: int) -> Trace:
        key = (kind, task_id)
        if key not in self.traces:
            while len(self.traces) >= MAX_TRACES:
                self.traces.pop(next(iter(self.traces)))
            self.traces[key] = Trace()
        return self.traces[key]

    def attach(self, kind: str, task: Any, executor: Any, prompt: Prompt) -> tuple[Any, Prompt]:
        """The task's executor and prompt factory with its playbooks and skills (unchanged while both are off)."""
        if not self._enabled():
            if self.skills is not None:
                return self.skills.attach(kind, task, executor, prompt)
            return executor, prompt
        trace = self.trace(kind, task.id)
        trace.new_run()
        if kind == "web task":
            where = web_where(tuple(self._user_sites()))
        else:
            where = screen_where(tuple(self._user_apps()), tuple(self._user_sites()), self._foreground)
        books = self.books
        loaded: list[str] = []  # the run's playbooks, read once: the prompt may be made again during the run

        async def with_playbooks() -> str:
            base = await prompt()
            if not loaded:
                try:
                    found = await books.named_in(task.instruction)
                except Exception:
                    log.warning("Couldn't read the playbooks for task “%s”", task.title, exc_info=True)
                    found = []
                trace.shown.update(book.id for book in found)
                trace.seen.update(book.place for book in found)
                loaded.append(block(found))
            return f"{base}\n\n{loaded[0]}" if loaded[0] else base

        wrapped: Any = PlaybookExecutor(executor, trace, books, where)
        if self.skills is None:
            return wrapped, with_playbooks

        def place() -> str | None:
            """Where the task is for the skills' record: a place's key, "" unknown, None somewhere private."""
            if trace.private:
                return None
            return trace.current.key if trace.current is not None else ""

        return self.skills.attach(kind, task, wrapped, with_playbooks, place=place)

    def finisher(self, kind: str) -> Callable[[Any], None]:
        """The task manager's ``finished`` hook: the task's write-up is queued (``PlaybookWriter.submit``)."""

        def finished(task: Any) -> None:
            if self.skills is not None:
                try:
                    self.skills.finished(kind, task)
                except Exception:
                    log.warning("The end of task “%s” wasn't passed to the skills", task.title, exc_info=True)
            trace = self.traces.pop((kind, task.id), None)
            if trace is None or not self._enabled():
                return
            outcome = task.status if task.status in ("done", "unfinished") else "failed"
            report = task.summary if task.status in ("done", "unfinished") else (task.error or "")
            try:
                session = self._session()
            except Exception:
                session = None
            self.writer.submit(Job(kind, task.title, task.instruction, outcome, report, trace, session))

        return finished
