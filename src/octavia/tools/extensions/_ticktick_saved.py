"""Whether an update TickTick answered was really saved: the task is read back and compared with what was asked.

TickTick's server can answer an update with the task as it was. Asked for a start date of Oct 21 and a due date of
Nov 4, it answered twice with both dates on Nov 4 and the same etag and modifiedTime: nothing had been saved, and
she had only the answer to go on. So after an update that sets a task's title, priority, dates or all-day flag, the
task is read back (``get_task_by_id``) and compared with what was asked. Dates it didn't keep are sent once more
with everything a span needs together: both dates in TickTick's own format, the all-day flag, the task's list and
time zone. The result then says what's saved, never just what was asked.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

from octavia.tools.extensions.canvas import _norm, _properties
from octavia.tools.extensions.ticktick import (
    Answer,
    RemoteTool,
    TickTickClient,
    TickTickError,
    _number,
    _pick,
    flat_schema,
    parse_tasks,
)

log = logging.getLogger(__name__)

# What's checked: our name for each field, its names on the wire (camelCase first, as TickTick answers), and how
# it's said.
FIELDS = {
    "title": (("title", "title"), "title"),
    "priority": (("priority", "priority"), "priority"),
    "start": (("startDate", "start_date"), "start date"),
    "due": (("dueDate", "due_date"), "due date"),
    "all_day": (("isAllDay", "is_all_day"), "all-day"),
}
DATES = ("start", "due")
TICKTICK_TIME = "%Y-%m-%dT%H:%M:%S.000+0000"  # how TickTick writes a moment: "2026-10-21T04:00:00.000+0000"


@dataclass(frozen=True)
class Checked:
    """How an update turned out: the arguments last sent, and the answer to give (the update's, with what was found
    saved added, or an error when it wasn't saved)."""

    arguments: dict[str, Any]
    answer: Answer
    saved: bool


def _holder(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    """Where the task's fields are: a nested ``task``, or the arguments themselves."""
    task = arguments.get("task")
    return task if isinstance(task, Mapping) else arguments


def asked(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The checked fields an update sets, by our names (the nested task's first, then the top level's)."""
    found: dict[str, Any] = {}
    for place in (_holder(arguments), arguments):
        for ours, (names, _) in FIELDS.items():
            if ours not in found and any(name in place for name in names):
                found[ours] = next(place[name] for name in names if name in place)
    return found


def _ids(arguments: Mapping[str, Any]) -> tuple[str, str]:
    task = _holder(arguments)
    ident = _pick(arguments, "task_id", "taskId") or _pick(task, "id", "task_id", "taskId")
    project = _pick(arguments, "project_id", "projectId") or _pick(task, "projectId", "project_id")
    return str(ident or ""), str(project or "")


def read_arguments(reader: RemoteTool, task_id: str, project_id: str) -> dict[str, Any] | None:
    """``get_task_by_id``'s arguments under the names its schema declares; None when it has no place for the id."""
    properties = _properties(flat_schema(reader.schema))
    if properties is None:
        return {"task_id": task_id, **({"project_id": project_id} if project_id else {})}
    out: dict[str, Any] = {}
    for key in properties:
        if _norm(key) in ("taskid", "id"):
            out.setdefault(key, task_id)
        elif _norm(key) == "projectid" and project_id:
            out[key] = project_id
    return out if task_id in out.values() else None


def moment(value: Any) -> datetime | date | None:
    """A date TickTick or the model wrote: a bare date, or a moment ("2026-10-21T04:00:00.000+0000", ISO with a
    colon or a Z); a moment without an offset is local time."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if len(text) == 10:
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    for form in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z"):
        try:
            return datetime.strptime(text, form)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def _days(value: datetime | date) -> set[date]:
    """The calendar days a date can mean: its own, and (for a moment) the local one, since an all-day task's date is
    midnight where the task lives, written in UTC."""
    if isinstance(value, datetime):
        return {value.date(), value.astimezone().date()}
    return {value}


def same_date(want: Any, have: Any, all_day: bool) -> bool:
    """Whether a saved date is the one asked for: the same moment, or the same day for an all-day task or a bare
    date."""
    if want in (None, ""):
        return have in (None, "")
    a, b = moment(want), moment(have)
    if a is None or b is None:
        return str(want).strip() == str(have or "").strip()
    if isinstance(a, datetime) and isinstance(b, datetime) and not all_day:
        return abs((a - b).total_seconds()) < 60
    return bool(_days(a) & _days(b))


def _have(saved: Mapping[str, Any]) -> dict[str, Any]:
    return {ours: _pick(saved, *names) for ours, (names, _) in FIELDS.items()}


def mismatches(want: Mapping[str, Any], saved: Mapping[str, Any]) -> dict[str, str]:
    """What TickTick didn't keep of what was asked, by our names, in words ("start date: asked ..., TickTick has
    ...")."""
    have = _have(saved)
    all_day = want.get("all_day", have["all_day"]) is True
    wrong = {}
    for ours, value in want.items():
        if ours in DATES:
            same = same_date(value, have[ours], all_day)
        elif ours == "all_day":
            same = bool(value) == (have[ours] is True)
        elif ours == "priority":
            same = _number(value) == _number(have[ours])
        else:
            same = " ".join(str(value or "").split()) == " ".join(str(have[ours] or "").split())
        if not same:
            wrong[ours] = f"{FIELDS[ours][1]}: asked {value!r}, TickTick has {have[ours]!r}"
    return wrong


def _day(found: datetime | date) -> date:
    """The day an all-day date means: as written when it's at midnight ("2026-10-21T00:00:00Z"), else the local day
    (TickTick writes local midnight in UTC: "2026-10-21T04:00:00.000+0000")."""
    if not isinstance(found, datetime):
        return found
    return found.date() if found.time() == time() else found.astimezone().date()


def _ticktick_time(value: Any, all_day: bool, like: Any) -> Any:
    """A date in TickTick's own form: a bare date where TickTick writes bare dates, else a UTC moment (an all-day
    date or a bare one meaning local midnight)."""
    found = moment(value)
    if found is None:
        return value
    if isinstance(like, str) and len(like.strip()) == 10:
        return _day(found).isoformat()
    if all_day or not isinstance(found, datetime):
        found = datetime.combine(_day(found), time()).astimezone()
    return found.astimezone(UTC).strftime(TICKTICK_TIME)


def fuller_update(
    schema: dict[str, Any], arguments: Mapping[str, Any], want: Mapping[str, Any], saved: Mapping[str, Any]
) -> dict[str, Any]:
    """The update again, with the dates in the same place as before and everything TickTick needs for them at once:
    both dates (the saved one where only one was asked), the all-day flag, and the task's id, list and time zone.
    Only fields the schema declares there are added (any, when it declares none)."""
    retry = copy.deepcopy(dict(arguments))
    nested = isinstance(retry.get("task"), Mapping)
    target: dict[str, Any] = retry["task"] if nested else retry
    properties = _properties(flat_schema(schema))
    if nested and properties is not None:
        properties = _properties(properties.get("task"))
    declared = {_norm(k): k for k in properties} if properties is not None else None
    snake = any(name in target for name in ("start_date", "due_date", "is_all_day", "project_id"))

    def put(camel: str, snake_name: str, value: Any, *, replace: bool) -> None:
        if value in (None, ""):
            return
        present = next((n for n in (camel, snake_name) if n in target), None)
        if present is not None:
            if replace:
                target[present] = value
            return
        name = snake_name if snake else camel
        if declared is not None:
            if _norm(name) not in declared:
                return
            name = declared[_norm(name)]
        target[name] = value

    have = _have(saved)
    all_day = want.get("all_day", have["all_day"]) is True
    for ours in DATES:
        value = want.get(ours, have[ours])
        camel, snake_name = FIELDS[ours][0]
        put(camel, snake_name, _ticktick_time(value, all_day, have[ours]), replace=True)
    put("isAllDay", "is_all_day", all_day, replace=False)
    if nested:
        put("id", "id", _pick(saved, "id"), replace=False)
    put("projectId", "project_id", _pick(saved, "projectId", "project_id"), replace=False)
    put("timeZone", "time_zone", _pick(saved, "timeZone", "time_zone"), replace=False)
    return retry


async def _read(client: TickTickClient, reader: RemoteTool, task_id: str, project_id: str) -> dict[str, Any] | None:
    """The task as TickTick has it saved, or None when it can't be read."""
    arguments = read_arguments(reader, task_id, project_id)
    if arguments is None:
        return None
    try:
        answer = await client.call(reader.name, arguments, retry=True)
    except TickTickError as exc:
        log.info("Couldn't read a TickTick task back: %s", exc)
        return None
    tasks = parse_tasks(answer.text) if answer.ok else []
    return next((t for t in tasks if str(t.get("id") or "") == task_id), tasks[0] if len(tasks) == 1 else None)


def _saved_words(saved: Mapping[str, Any], want: Mapping[str, Any]) -> str:
    return ", ".join(f"{FIELDS[ours][1]} {_pick(saved, *FIELDS[ours][0])!r}" for ours in want)


async def check_update(
    client: TickTickClient,
    updater: RemoteTool,
    reader: RemoteTool,
    arguments: dict[str, Any],
    answer: Answer,
) -> Checked:
    """Read back the task an update went through for, and say what's really saved; dates it didn't keep are sent
    once more (``fuller_update``). An update that sets none of the checked fields is left as it is."""
    want = asked(arguments)
    task_id, project_id = _ids(arguments)
    if not want or not task_id:
        return Checked(arguments, answer, saved=True)
    saved = await _read(client, reader, task_id, project_id)
    if saved is None:
        note = "\n(TickTick couldn't be asked whether this was saved: don't say it's done for sure.)"
        return Checked(arguments, Answer(answer.text + note, answer.ok), saved=True)
    wrong = mismatches(want, saved)
    if not wrong:
        note = f"\nChecked: TickTick has it saved ({_saved_words(saved, want)})."
        return Checked(arguments, Answer(answer.text + note), saved=True)
    retry = fuller_update(updater.schema, arguments, want, saved)
    if any(ours in DATES for ours in wrong) and retry != arguments:
        try:
            again = await client.call(updater.name, retry)
        except TickTickError as exc:
            log.info("Couldn't send a TickTick update again: %s", exc)
            again = Answer(str(exc), ok=False)
        if again.ok and (saved_now := await _read(client, reader, task_id, project_id)) is not None:
            saved, wrong = saved_now, mismatches(want, saved_now)
            if not wrong:
                note = (
                    "\nChecked: TickTick hadn't kept the dates the first time, so they were sent again together; "
                    f"now it has them saved ({_saved_words(saved, want)})."
                )
                return Checked(retry, Answer(again.text + note), saved=True)
    return Checked(
        arguments,
        Answer(
            "TickTick answered the update, but reading the task back shows it didn't keep it ("
            + "; ".join(wrong.values())
            + "). Its answer to the update can't be trusted: tell the user it didn't change rather than that it's "
            "done.",
            ok=False,
        ),
        saved=False,
    )
