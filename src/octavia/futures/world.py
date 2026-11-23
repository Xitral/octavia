"""The world model: the things in the user's life Octavia knows about, and how they hang together.

An entity is anything the event stream mentions that is worth keeping track of: a to-do, an assignment and its
course, a person who emails, an app the user works in, one of her own tools. Each has a reference (``task:65a1...``,
``application:blender``, see ``schema.ref``), a name, a status and a few attributes (a due time, when it was last
used). Relations say how they hang together: a to-do *belongs to* a project, the user is *waiting on* someone. A
relation may point at something with no entity of its own yet (a TickTick project she only knows by its id).

Everything here is written by plain rules from events (``WorldModel.apply``), never guessed by a model, so it is
facts, not beliefs. What it keeps of their history is only what matters later: a changed name, status, title or due
time goes to ``entity_changes`` ("the lab report was pushed back twice"). The user can forget one thing, with its
relations and history, or all of it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from octavia.futures.events import FuturesEvent
from octavia.futures.privacy import redact
from octavia.futures.schema import EVENT_TYPES, Sensitivity, split_ref
from octavia.futures.schema import ref as make_ref
from octavia.futures.store import FuturesStore, dumps, loads

ENTITY_KINDS = frozenset(
    {
        "user",
        "person",
        "goal",
        "project",
        "task",
        "commitment",
        "course",
        "assignment",
        "organization",
        "conversation",
        "routine",
        "application",
        "file",
        "skill",
        "model",
        "experiment",
        "hypothesis",
        "prediction",
        "timer",
        "improvement",
        "email",
    }
)
PREDICATES = frozenset(
    {
        "contributes_to",
        "belongs_to",
        "associated_with",
        "concerns",
        "evaluates",
        "blocked_by",
        "waiting_on",
        "part_of",
        "created_by",
        "used",
    }
)
TRACKED = frozenset({"name", "status", "due_ts", "title"})  # changes to these go to entity_changes
# Work in one of these states is no longer waiting to be done.
DONE = frozenset({"completed", "deleted", "submitted", "graded"})

_TASK_ATTRS = ("due_ts", "all_day", "priority", "project_id")
_CANVAS_ATTRS = ("due_ts", "kind", "course")
_TIMER_STATUS = {"timer.created": "set", "timer.completed": "rang", "timer.cancelled": "cancelled"}
_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class Entity:
    ref: str
    kind: str
    name: str
    status: str | None
    attrs: Mapping[str, Any]
    domain: str
    sensitivity: str
    source: str
    first_seen: float
    last_seen: float
    last_event: str | None

    @property
    def key(self) -> str:
        return split_ref(self.ref)[1]

    @property
    def due_ts(self) -> float | None:
        due = self.attrs.get("due_ts")
        return float(due) if isinstance(due, int | float) else None


@dataclass(frozen=True)
class Relation:
    subject: str
    predicate: str
    object: str
    attrs: Mapping[str, Any]
    created_at: float
    updated_at: float
    event_id: str | None


@dataclass(frozen=True)
class EntityChange:
    ref: str
    field: str
    old: Any
    new: Any
    ts: float
    event_id: str | None


def _to_entity(row: Any) -> Entity:
    return Entity(
        ref=row["ref"],
        kind=row["kind"],
        name=row["name"],
        status=row["status"],
        attrs=MappingProxyType(loads(row["attrs"], {})),
        domain=row["domain"],
        sensitivity=row["sensitivity"],
        source=row["source"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        last_event=row["last_event"],
    )


def _to_relation(row: Any) -> Relation:
    return Relation(
        subject=row["subject"],
        predicate=row["predicate"],
        object=row["object"],
        attrs=MappingProxyType(loads(row["attrs"], {})),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        event_id=row["event_id"],
    )


def _to_change(row: Any) -> EntityChange:
    return EntityChange(
        ref=row["ref"],
        field=row["field"],
        old=loads(row["old"]),
        new=loads(row["new"]),
        ts=row["ts"],
        event_id=row["event_id"],
    )


def normalize_ref(value: str) -> str:
    """``value`` as the world model stores it (``application:Blender`` is ``application:blender``); a ValueError
    for a kind it doesn't know or a missing key."""
    kind, key = split_ref(str(value))
    if kind not in ENTITY_KINDS:
        raise ValueError(f"unknown entity kind {kind!r} in {value!r}")
    return make_ref(kind, key)


def _merge(attrs: Mapping[str, Any], changes: Mapping[str, Any] | None) -> dict[str, Any]:
    """``attrs`` with ``changes`` applied key by key; a None value removes the key."""
    merged = dict(attrs)
    for key, value in (changes or {}).items():
        if value is None:
            merged.pop(str(key), None)
        else:
            merged[str(key)] = value
    return merged


class WorldModel:
    """The entities, relations and entity_changes tables."""

    def __init__(self, store: FuturesStore) -> None:
        self.store = store

    # ------------------------------------------------------------------ entities
    def upsert(
        self,
        ref: str,
        *,
        source: str,
        name: str | None = None,
        status: str | None = None,
        attrs: Mapping[str, Any] | None = None,
        domain: str | None = None,
        sensitivity: str | None = None,
        event_id: str | None = None,
        ts: float | None = None,
    ) -> Entity:
        """Create the entity ``ref`` or merge what's given into it: attrs key by key (None removes one), and
        anything left out stays as it was. On an update, a changed name, status, due time or title is noted in
        ``entity_changes``."""
        ref = normalize_ref(ref)
        kind, key = split_ref(ref)
        ts = self.store.clock() if ts is None else ts
        name = redact(str(name), 200) if name else None
        if sensitivity is not None:
            sensitivity = Sensitivity(sensitivity).value
        with self.store.transaction():
            row = self.store.one("SELECT * FROM entities WHERE ref=?", (ref,))
            if row is None:
                self.store.execute(
                    "INSERT INTO entities(ref, kind, name, status, attrs, domain, sensitivity, source, first_seen, "
                    "last_seen, last_event) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        ref,
                        kind,
                        name or key,
                        status,
                        dumps(_merge({}, attrs)),
                        domain or "general",
                        sensitivity or Sensitivity.PERSONAL.value,
                        source,
                        ts,
                        ts,
                        event_id,
                    ),
                )
            else:
                old = _to_entity(row)
                merged = _merge(old.attrs, attrs)
                new_name = name or old.name
                new_status = old.status if status is None else status
                placeholder = old.name == key  # a name that was only the key getting its real one isn't a rename
                tracked = [
                    ("name", None if placeholder else old.name, None if placeholder else new_name),
                    ("status", old.status, new_status),
                ]
                tracked += [(k, old.attrs.get(k), merged.get(k)) for k in sorted(TRACKED - {"name", "status"})]
                for field, before, after in tracked:
                    if before != after:
                        self._note_change(ref, field, before, after, ts, event_id)
                # An event replayed late doesn't make the entity look older than it is.
                newer = ts >= old.last_seen
                self.store.execute(
                    "UPDATE entities SET name=?, status=?, attrs=?, domain=?, sensitivity=?, source=?, last_seen=?, "
                    "last_event=? WHERE ref=?",
                    (
                        new_name,
                        new_status,
                        dumps(merged),
                        domain or old.domain,
                        sensitivity or old.sensitivity,
                        source,
                        max(ts, old.last_seen),
                        (event_id or old.last_event) if newer else old.last_event,
                        ref,
                    ),
                )
            entity = self.get(ref)
        assert entity is not None
        return entity

    def _note_change(self, ref: str, field: str, old: Any, new: Any, ts: float, event_id: str | None) -> None:
        self.store.execute(
            "INSERT INTO entity_changes(ref, field, old, new, ts, event_id) VALUES (?,?,?,?,?,?)",
            (ref, field, None if old is None else dumps(old), None if new is None else dumps(new), ts, event_id),
        )

    def get(self, ref: str) -> Entity | None:
        try:
            ref = normalize_ref(ref)
        except ValueError:
            return None
        row = self.store.one("SELECT * FROM entities WHERE ref=?", (ref,))
        return _to_entity(row) if row else None

    def find(
        self,
        kind: str | None = None,
        *,
        status: str | Iterable[str] | None = None,
        name: str | None = None,
        limit: int = 50,
    ) -> list[Entity]:
        """Entities newest seen first, of one kind, in one of the given statuses, and with ``name`` anywhere in
        theirs (ignoring case)."""
        clauses, params = ["1=1"], []
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind)
        if status is not None:
            statuses = [status] if isinstance(status, str) else list(status)
            clauses.append(f"status IN ({','.join('?' * len(statuses))})" if statuses else "0")
            params.extend(statuses)
        sql = f"SELECT * FROM entities WHERE {' AND '.join(clauses)} ORDER BY last_seen DESC, ref"
        if name is None:
            return [_to_entity(r) for r in self.store.execute(f"{sql} LIMIT ?", (*params, limit))]
        # In Python rather than LIKE: SQLite only folds the case of ASCII letters.
        needle = " ".join(name.split()).casefold()
        found = []
        for row in self.store.execute(sql, params):
            if needle in row["name"].casefold():
                found.append(_to_entity(row))
                if len(found) >= limit:
                    break
        return found

    def forget(self, ref: str) -> bool:
        """Delete the entity, every relation to or from it and its change history. False when there was nothing."""
        try:
            ref = normalize_ref(ref)
        except ValueError:
            return False
        with self.store.transaction():
            removed = self.store.changed("DELETE FROM entities WHERE ref=?", (ref,))
            removed += self.store.changed("DELETE FROM relations WHERE subject=? OR object=?", (ref, ref))
            removed += self.store.changed("DELETE FROM entity_changes WHERE ref=?", (ref,))
        return removed > 0

    def delete_all(self) -> int:
        """Forget the whole world model. Returns how many entities and relations there were."""
        with self.store.transaction():
            removed = self.store.changed("DELETE FROM entities")
            removed += self.store.changed("DELETE FROM relations")
            self.store.changed("DELETE FROM entity_changes")
        return removed

    # ------------------------------------------------------------------ relations
    def relate(
        self,
        subject: str,
        predicate: str,
        obj: str,
        *,
        attrs: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        ts: float | None = None,
    ) -> Relation:
        """Note that ``subject`` ``predicate`` ``obj`` (``task:1 belongs_to project:inbox``). Relating the same
        triple again merges ``attrs`` into the relation (None removes a key)."""
        if predicate not in PREDICATES:
            raise ValueError(f"unknown relation {predicate!r}")
        subject, obj = normalize_ref(subject), normalize_ref(obj)
        ts = self.store.clock() if ts is None else ts
        with self.store.transaction():
            row = self._relation_row(subject, predicate, obj)
            if row is None:
                self.store.execute(
                    "INSERT INTO relations(subject, predicate, object, attrs, created_at, updated_at, event_id) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (subject, predicate, obj, dumps(_merge({}, attrs)), ts, ts, event_id),
                )
            else:
                old = _to_relation(row)
                self.store.execute(
                    "UPDATE relations SET attrs=?, updated_at=?, event_id=? WHERE id=?",
                    (dumps(_merge(old.attrs, attrs)), max(ts, old.updated_at), event_id or old.event_id, row["id"]),
                )
            relation = self._relation_row(subject, predicate, obj)
        assert relation is not None
        return _to_relation(relation)

    def _relation_row(self, subject: str, predicate: str, obj: str) -> Any:
        return self.store.one(
            "SELECT * FROM relations WHERE subject=? AND predicate=? AND object=?", (subject, predicate, obj)
        )

    def unrelate(self, subject: str, predicate: str, obj: str) -> bool:
        try:
            subject, obj = normalize_ref(subject), normalize_ref(obj)
        except ValueError:
            return False
        return (
            self.store.changed(
                "DELETE FROM relations WHERE subject=? AND predicate=? AND object=?", (subject, predicate, obj)
            )
            > 0
        )

    def related(
        self, ref: str, predicate: str | None = None, *, direction: Literal["out", "in", "both"] = "out"
    ) -> list[Relation]:
        """The relations from ``ref`` (``"out"``), to it (``"in"``) or both, newest updated first."""
        try:
            ref = normalize_ref(ref)
        except ValueError:
            return []
        ends = {"out": "subject=?", "in": "object=?", "both": "(subject=? OR object=?)"}
        if direction not in ends:
            raise ValueError(f"unknown direction {direction!r}")
        clauses, params = [ends[direction]], [ref] * (2 if direction == "both" else 1)
        if predicate is not None:
            clauses.append("predicate=?")
            params.append(predicate)
        rows = self.store.execute(
            f"SELECT * FROM relations WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, id DESC", params
        )
        return [_to_relation(r) for r in rows]

    # ------------------------------------------------------------------ history
    def changes(
        self, *, since: float, until: float | None = None, field: str | None = None, kind: str | None = None
    ) -> list[EntityChange]:
        """What changed about entities from ``since`` (until before ``until``), oldest first."""
        clauses, params = ["ts>=?"], [since]
        if until is not None:
            clauses.append("ts<?")
            params.append(until)
        if field is not None:
            clauses.append("field=?")
            params.append(field)
        if kind is not None:
            clauses.append("substr(ref, 1, ?)=?")
            params.extend((len(kind) + 1, f"{kind}:"))
        rows = self.store.execute(f"SELECT * FROM entity_changes WHERE {' AND '.join(clauses)} ORDER BY ts, id", params)
        return [_to_change(r) for r in rows]

    # ------------------------------------------------------------------ the rules
    def apply(self, event: FuturesEvent) -> list[str]:
        """Update the world from ``event`` by the rules for its type. Returns the refs of the entities it created or
        updated; an event with nothing to say about the world returns []."""
        rule = _RULES.get(event.type) or (_build if event.type.startswith("build.") else None)
        if rule is None:
            return []
        with self.store.transaction():
            touched = rule(_Apply(self, event))
        return list(dict.fromkeys(touched))

    # ------------------------------------------------------------------ queries
    def deadlines_changed(self, since: float) -> list[tuple[Entity, EntityChange]]:
        """Due times of to-dos and assignments that moved since ``since``, oldest first, with the entity as it is
        now."""
        found = []
        for change in self.changes(since=since, field="due_ts"):
            if split_ref(change.ref)[0] not in ("task", "assignment"):
                continue
            entity = self.get(change.ref)
            if entity is not None:
                found.append((entity, change))
        return found

    def open_with_due(
        self, *, kinds: Iterable[str] = ("task", "assignment"), before: float | None = None
    ) -> list[Entity]:
        """Work still to do that has a due time (before ``before``, when given), soonest due first."""
        kinds = list(kinds)
        if not kinds:
            return []
        rows = self.store.execute(f"SELECT * FROM entities WHERE kind IN ({','.join('?' * len(kinds))})", kinds)
        found = []
        for row in rows:
            entity = _to_entity(row)
            due = entity.due_ts
            if entity.status in DONE or due is None or (before is not None and due >= before):
                continue
            found.append(entity)
        return sorted(found, key=lambda e: (e.due_ts, e.ref))

    def blocked(self) -> list[tuple[Entity, list[Relation]]]:
        """What's stuck: entities with status "blocked" or something they're blocked by, with those relations.
        Finished work isn't stuck any more, whatever its old relations say."""
        rows = self.store.execute(
            "SELECT * FROM entities WHERE status='blocked' OR ref IN "
            "(SELECT subject FROM relations WHERE predicate='blocked_by') ORDER BY last_seen DESC, ref"
        )
        found = []
        for row in rows:
            entity = _to_entity(row)
            if entity.status in DONE:
                continue
            found.append((entity, self.related(entity.ref, "blocked_by")))
        return found

    def waiting_on(self) -> list[Relation]:
        """Everything the user is waiting on someone for, oldest first; ``attrs["replied_ts"]`` once they replied."""
        rows = self.store.execute("SELECT * FROM relations WHERE predicate='waiting_on' ORDER BY created_at, id")
        return [_to_relation(r) for r in rows]


# ---------------------------------------------------------------------------------------------- the rules
class _Apply:
    """One event being applied: what every rule needs to know about it."""

    def __init__(self, world: WorldModel, event: FuturesEvent) -> None:
        self.world = world
        self.event = event
        self.ts = event.ts or world.store.clock()
        self.data: Mapping[str, Any] = event.data or _EMPTY
        known = EVENT_TYPES.get(event.type)
        self.domain = event.domain or (known.domain.value if known else "general")
        # A sensitive event makes its entity sensitive; an ordinary one never makes it less so.
        self.sensitive = (event.sensitivity or (known.sensitivity.value if known else "")) == "sensitive"

    def subject(self, kind: str) -> str | None:
        """The event's subject when it's an entity of ``kind``."""
        if not self.event.subject:
            return None
        try:
            ref = normalize_ref(self.event.subject)
        except ValueError:
            return None
        return ref if split_ref(ref)[0] == kind else None

    def named(self, kind: str, key: Any) -> str | None:
        """The ref of the ``kind`` entity called ``key``, or None when there's no key."""
        key = " ".join(str(key or "").split())
        return make_ref(kind, key) if key else None

    def upsert(self, ref: str, *, subject: bool = True, **fields: Any) -> str:
        if subject and self.sensitive and "sensitivity" not in fields:
            fields["sensitivity"] = Sensitivity.SENSITIVE.value
        self.world.upsert(
            ref, source=self.event.source, domain=self.domain, event_id=self.event.id or None, ts=self.ts, **fields
        )
        return ref

    def relate(self, subject: str, predicate: str, obj: str, attrs: Mapping[str, Any] | None = None) -> None:
        self.world.relate(subject, predicate, obj, attrs=attrs, event_id=self.event.id or None, ts=self.ts)

    def exists(self, ref: str) -> Entity | None:
        return self.world.get(ref)

    def given(self, keys: Iterable[str], *, keep_none: bool) -> dict[str, Any]:
        """The ``keys`` the event's data carries; a None among them removes the attribute only with ``keep_none``."""
        return {k: self.data[k] for k in keys if k in self.data and (keep_none or self.data[k] is not None)}


def _task(a: _Apply) -> list[str]:
    ref = a.subject("task")
    if ref is None:
        return []
    kind = a.event.type
    if kind == "task.updated":
        # An update names what it changed: a None there is "not said", except a due time that was taken away.
        attrs = a.given(_TASK_ATTRS, keep_none=False)
        if "due_ts" in a.data and a.data["due_ts"] is None and a.data.get("old_due_ts") is not None:
            attrs["due_ts"] = None
        due = attrs.get("due_ts")
        if isinstance(due, int | float) and due > a.ts:
            attrs["overdue"] = None  # pushed back into the future: not overdue any more
    else:
        attrs = a.given(_TASK_ATTRS, keep_none=True)
    if kind == "task.created" and a.data.get("by"):
        attrs["created_by"] = a.data["by"]
    a.upsert(ref, name=a.data.get("title") or None, status="open", attrs=attrs)
    project = a.data.get("project_id")
    if project:
        project_ref = a.named("project", project)
        assert project_ref is not None
        # A to-do is in one project at a time: a move leaves the old one.
        for old in a.world.related(ref, "belongs_to"):
            if old.object.startswith("project:") and old.object != project_ref:
                a.world.unrelate(ref, "belongs_to", old.object)
        a.relate(ref, "belongs_to", project_ref)
    return [ref]


def _task_completed(a: _Apply) -> list[str]:
    ref = a.subject("task")
    if ref is None:
        return []
    done = a.data.get("completed_ts")
    attrs: dict[str, Any] = {"completed_ts": done if isinstance(done, int | float) else a.ts}
    attrs.update(a.given(("due_ts",), keep_none=False))
    return [a.upsert(ref, name=a.data.get("title") or None, status="completed", attrs=attrs)]


def _task_deleted(a: _Apply) -> list[str]:
    ref = a.subject("task")
    if ref is None:
        return []
    return [a.upsert(ref, name=a.data.get("title") or None, status="deleted")]


def _task_overdue(a: _Apply) -> list[str]:
    ref = a.subject("task")
    if ref is None:
        return []
    attrs = {"overdue": True, **a.given(("due_ts",), keep_none=False)}
    status = None if a.exists(ref) else "open"  # only an undone to-do can be overdue
    return [a.upsert(ref, name=a.data.get("title") or None, status=status, attrs=attrs)]


def _assignment(a: _Apply) -> list[str]:
    ref = a.subject("assignment")
    if ref is None:
        return []
    touched = []
    status: str | None = "open"
    if a.event.type == "canvas.assignment_updated":
        current = a.exists(ref)
        if current is not None and current.status in ("submitted", "graded"):
            status = None  # a new due date doesn't un-submit it
    attrs = a.given(_CANVAS_ATTRS, keep_none=a.event.type == "canvas.assignment_created")
    touched.append(a.upsert(ref, name=a.data.get("title") or None, status=status, attrs=attrs))
    touched.extend(_course(a, ref))
    return touched


def _course(a: _Apply, assignment: str) -> list[str]:
    course = a.named("course", a.data.get("course"))
    if course is None:
        return []
    a.upsert(course, subject=False, name=str(a.data["course"]))
    a.relate(assignment, "belongs_to", course)
    return [course]


def _submitted(a: _Apply) -> list[str]:
    ref = a.subject("assignment")
    if ref is None:
        return []
    return [a.upsert(ref, name=a.data.get("title") or None, status="submitted"), *_course(a, ref)]


def _graded(a: _Apply) -> list[str]:
    ref = a.subject("assignment")
    if ref is None:
        return []
    attrs = {"grade_band": a.data.get("band"), **a.given(("late",), keep_none=False)}
    a.upsert(
        ref,
        name=a.data.get("title") or None,
        status="graded",
        attrs=attrs,
        sensitivity=Sensitivity.SENSITIVE.value,
    )
    return [ref, *_course(a, ref)]


def _email(a: _Apply) -> list[str]:
    sender = a.data.get("sender")
    person = a.named("person", sender)
    if person is None:
        return []
    a.upsert(person, name=str(sender), attrs={"last_email_ts": a.ts})
    for waiting in a.world.related(person, "waiting_on", direction="in"):
        # The first mail from them since the user started waiting is the reply; to wait again, relate it again
        # with replied_ts=None.
        if "replied_ts" not in waiting.attrs and waiting.created_at <= a.ts:
            a.relate(waiting.subject, "waiting_on", person, {"replied_ts": a.ts})
    return [person]


def _application(a: _Apply) -> list[str]:
    app = a.data.get("app") or (split_ref(a.event.subject)[1] if a.event.subject else "")
    ref = a.named("application", app)
    if ref is None:
        return []
    if a.event.type == "computer.application_closed":
        attrs: dict[str, Any] = {"closed_ts": a.ts}
    else:
        attrs = {"last_used_ts": a.ts, **a.given(("process",), keep_none=False)}
    return [a.upsert(ref, name=str(app), attrs=attrs)]


def _skill(a: _Apply) -> list[str]:
    tool = a.data.get("tool") or (split_ref(a.event.subject)[1] if a.event.subject else "")
    ref = a.named("skill", tool)
    if ref is None:
        return []
    attrs = {"last_used_ts": a.ts, **a.given(("category",), keep_none=False)}
    return [a.upsert(ref, name=str(tool), attrs=attrs)]


def _goal(a: _Apply) -> list[str]:
    ref = a.subject("goal")
    if ref is None:
        return []
    text = a.data.get("text") or None
    if a.event.type == "goal.created":
        return [a.upsert(ref, name=text, status="active")]
    if a.event.type == "goal.completed":
        return [a.upsert(ref, name=text, status="completed")]
    return [a.upsert(ref, name=text, attrs={"last_progress_ts": a.ts})]


def _build(a: _Apply) -> list[str]:
    ref = a.subject("improvement")
    if ref is None:
        return []
    status = a.data.get("status") or a.event.type.partition(".")[2]
    return [a.upsert(ref, name=a.data.get("title") or None, status=str(status))]


def _model(a: _Apply) -> list[str]:
    model = a.data.get("model")
    ref = a.named("model", model)
    if ref is None:
        return []
    return [a.upsert(ref, name=str(model), attrs={"last_used_ts": a.ts})]


def _timer(a: _Apply) -> list[str]:
    ref = a.subject("timer")
    if ref is None:
        return []
    label = a.data.get("label") or None
    if label is None and (a.event.type == "timer.created" or a.exists(ref) is None):
        label = "timer"
    attrs = a.given(("seconds",), keep_none=False) if a.event.type == "timer.created" else None
    return [a.upsert(ref, name=label, status=_TIMER_STATUS[a.event.type], attrs=attrs)]


_RULES = {
    "task.created": _task,
    "task.updated": _task,
    "task.seen": _task,
    "task.completed": _task_completed,
    "task.deleted": _task_deleted,
    "task.overdue": _task_overdue,
    "canvas.assignment_created": _assignment,
    "canvas.assignment_updated": _assignment,
    "canvas.assignment_submitted": _submitted,
    "canvas.grade_posted": _graded,
    "gmail.message_received": _email,
    "computer.application_opened": _application,
    "computer.context_changed": _application,
    "computer.application_closed": _application,
    "computer.action_performed": _skill,
    "computer.action_failed": _skill,
    "goal.created": _goal,
    "goal.progress": _goal,
    "goal.completed": _goal,
    "assistant.reply": _model,
    "timer.created": _timer,
    "timer.completed": _timer,
    "timer.cancelled": _timer,
}
