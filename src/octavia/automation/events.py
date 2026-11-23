"""The one event schema every watcher publishes in.

A ``BusEvent`` has a dotted ``type`` ("gmail.received"), the ``source`` that saw it, when it happened, a small
``payload``, its ``provenance`` (a watcher, or the rule whose action caused it) and how ``sensitive`` it is.
``transient`` holds what may be matched live but is never written to the log (an email's subject, a phone
notification's text): a rule can still say "when an email about invoices arrives", and nothing private is kept.
``chain`` lists the rules whose actions led to it, which is how chain depth and loops are caught.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

SENSITIVITIES = ("normal", "personal", "sensitive")

# The event types published so far, with what they carry. A rule may name others (a pattern such as "build.*", or a
# type a later watcher adds), but these are the ones the model is told about.
KNOWN_TYPES: dict[str, str] = {
    "gmail.received": "new inbox mail: sender, unread; subject (live only)",
    "app.opened": "an app came to the front: app, process, previous",
    "ticktick.created": "a to-do was added: title, due, priority",
    "ticktick.updated": "a to-do changed: title, due, priority",
    "ticktick.completed": "a to-do was ticked off: title",
    "ticktick.deleted": "a to-do was deleted: title",
    "ticktick.all_done": "the last to-do due today was ticked off",
    "timer.created": "a timer was set: label, seconds",
    "timer.rang": "a timer rang: label, seconds",
    "timer.cancelled": "a timer was cancelled: label",
    "build.started": "a self-improvement build began: id, title",
    "build.finished": "a build ended: id, title, status (ready, failed, no_change, rejected), ok",
    "build.installed": "a build was installed: id, title",
    "build.rolled_back": "an installed build was undone: id, title",
    "lead.added": "a lead was added: company, stage",
    "lead.stage_changed": "a lead moved stage: lead, stage",
    "phone.notification": "phone notifications were said: text (live only)",
    "rule.failed": "one of your rules' actions failed: rule, name, error",
}


@dataclass(frozen=True)
class BusEvent:
    type: str
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    provenance: str = "watcher"
    sensitivity: str = "normal"
    transient: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    chain: tuple[int, ...] = ()
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def get(self, path: str) -> Any:
        """A field by name: ``sender`` or ``payload.sender`` (the payload, then what's live only), or one of the
        event's own (``type``, ``source``, ``sensitivity``, ``provenance``). None when it isn't there."""
        if path in ("type", "source", "sensitivity", "provenance", "ts"):
            return getattr(self, path)
        for prefix, where in (("payload.", self.payload), ("transient.", self.transient)):
            if path.startswith(prefix):
                return where.get(path[len(prefix) :])
        if path in self.payload:
            return self.payload[path]
        return self.transient.get(path)

    @property
    def depth(self) -> int:
        return len(self.chain)
